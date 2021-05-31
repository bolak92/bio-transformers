"""
This script defines a parent class for transformers, for which child classes which are
specific to a given transformers implementation can inherit.
It allows to derive probabilities, embeddings and log-likelihoods based on inputs
sequences, and displays some properties of the transformer model.
"""
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple, Union

import numpy as np
import pytorch_lightning as pl
import torch
import torch.tensor
from biotransformers.utils.constant import NATURAL_AAS_LIST
from biotransformers.utils.gpus_utils import set_device
from biotransformers.utils.utils import (
    _check_memory_embeddings,
    _check_memory_logits,
    _check_sequence,
    get_logs_version,
    load_fasta,
)
from torch.nn import functional as F  # noqa: N812
from tqdm import tqdm


class TransformersWrapper(ABC):
    """
    Abstract class that uses pretrained transformers model to evaluate
    a protein likelihood so as other insights.
    """

    def __init__(
        self,
        model_dir: str,
        _device: Optional[str] = None,
        multi_gpu: bool = False,
        mask_bool: bool = False,
    ):
        """Initialize Transformers wrapper

        Args:
            model_dir: name directory of the pretrained model
            _device: type of device to use (cpu or cuda).
            multi_gpu: turn on to True to use multigpu
            mask_bool: Wether to use mask or not for inference.
        """
        _device, _multi_gpu = set_device(_device, multi_gpu)

        self._device = torch.device(_device)
        self.multi_gpu = _multi_gpu
        self.model_dir = model_dir
        self.mask_bool = mask_bool

    @property
    def model_id(self) -> str:
        """Model ID, as specified in the model directory"""
        return self.model_dir.lower()

    @property
    @abstractmethod
    def clean_model_id(self) -> str:
        """Clean model ID (in case the model directory is not)"""

    @property
    @abstractmethod
    def model_vocabulary(self) -> List[str]:
        """Returns the whole vocabulary list"""

    @property
    @abstractmethod
    def vocab_size(self) -> int:
        """Returns the whole vocabulary size"""

    @property
    @abstractmethod
    def mask_token(self) -> str:
        """Representation of the mask token (as a string)"""

    @property
    @abstractmethod
    def pad_token(self) -> str:
        """Representation of the pad token (as a string)"""

    @property
    @abstractmethod
    def begin_token(self) -> str:
        """Representation of the beginning of sentence token (as a string).
        Returns an empty string if no such token"""

    @property
    @abstractmethod
    def end_token(self) -> str:
        """Representation of the end of sentence token (as a string).
        Returns an empty string if no such token."""

    @property
    @abstractmethod
    def does_end_token_exist(self) -> bool:
        """Returns true if a end of sequence token exists"""

    @property
    @abstractmethod
    def token_to_id(self):
        """Returns a function which maps tokens to IDs"""

    @property
    @abstractmethod
    def embeddings_size(self) -> int:
        """Returns size of the embeddings"""

    @abstractmethod
    def _process_sequences_and_tokens(
        self, sequences_list: List[str], tokens_list: List[str]
    ) -> Tuple[Dict[str, torch.tensor], torch.tensor, List[int]]:
        """Function to transform tokens string to IDs; it depends on the model used"""
        return NotImplemented, NotImplemented, NotImplemented

    @abstractmethod
    def _model_pass(
        self, model_inputs: Dict[str, torch.tensor]
    ) -> Tuple[torch.tensor, torch.tensor]:
        """Function which computes logits and embeddings based on a list of sequences"""
        return NotImplemented, NotImplemented

    def _get_num_batch_iter(self, model_inputs: Dict[str, Any], batch_size: int) -> int:
        num_of_sequences = model_inputs["input_ids"].shape[0]
        num_batch_iter = int(np.ceil(num_of_sequences / batch_size))
        return num_batch_iter

    def _generate_chunks(
        self, model_inputs: Dict[str, Any], batch_size: int
    ) -> Generator[Dict[str, Iterable], None, None]:
        """Yield a dictionnary of tensor"""
        num_of_sequences = model_inputs["input_ids"].shape[0]
        for i in range(0, num_of_sequences, batch_size):
            batch_sequence = {
                key: value[i : (i + batch_size)] for key, value in model_inputs.items()
            }
            yield batch_sequence

    def _repeat_and_mask_inputs(
        self, model_inputs: Dict[str, torch.Tensor]
    ) -> Tuple[Dict[str, torch.Tensor], List[List]]:
        """Create new tensor by masking each token and repeating sequence

        Args:
            model_inputs: shape -> (num_seqs, max_seq_len)

        Returns:
            model_inputs: shape -> (sum_tokens, max_seq_len)
            masked_ids_list: len -> (num_seqs)
        """
        new_input_ids = []
        new_attention_mask = []
        new_token_type_ids = []
        mask_ids = []
        for sequence, binary_mask, zeros in zip(
            model_inputs["input_ids"],
            model_inputs["attention_mask"],
            model_inputs["token_type_ids"],
        ):
            mask_id = []
            for i in range(1, sum(binary_mask) - self.does_end_token_exist * 1):
                mask_sequence = torch.tensor(
                    sequence[:i].tolist()
                    + [self.token_to_id(self.mask_token)]
                    + sequence[i + 1 :].tolist(),
                    dtype=torch.int64,
                )
                new_input_ids.append(mask_sequence)
                new_attention_mask.append(binary_mask)
                new_token_type_ids.append(zeros)
                mask_id.append(i)
            mask_ids.append(mask_id)
        model_inputs["input_ids"] = torch.stack(new_input_ids)
        model_inputs["attention_mask"] = torch.stack(new_attention_mask)
        model_inputs["token_type_ids"] = torch.stack(new_token_type_ids)
        return model_inputs, mask_ids

    def _gather_masked_outputs(
        self, model_outputs: torch.Tensor, masked_ids_list: List[List]
    ) -> torch.Tensor:
        """Gather all the masked outputs to get original tensor shape

        Args:
            model_outputs (torch.Tensor): shape -> (sum_tokens, max_seq_len, vocab_size)
            masked_ids_list (List[List]) : len -> (num_seqs)

        Returns:
            model_outputs (torch.Tensor): shape -> (num_seqs, max_seq_len, vocab_size)
        """
        max_length = model_outputs.shape[1]
        inf_tensor = -float("Inf") * torch.ones(
            [1, model_outputs.shape[2]], dtype=torch.float32
        )
        sequences_list = []
        start_id = 0
        for mask_id in masked_ids_list:
            end_id = start_id + len(mask_id)
            sequence = torch.cat(
                (
                    inf_tensor,
                    model_outputs[range(start_id, end_id), mask_id],
                    inf_tensor.repeat(max_length - len(mask_id) - 1, 1),
                ),
                0,
            )
            sequences_list.append(sequence)
            start_id = end_id
        return torch.stack(sequences_list)

    def _labels_remapping(
        self, labels: torch.Tensor, tokens: List[int]
    ) -> torch.Tensor:
        """Function that remaps IDs of the considered tokens from 0 to len(tokens)"""
        mapping = dict(zip(tokens, range(len(tokens))))
        return torch.tensor([mapping[lbl.item()] for lbl in labels])

    def _label_remapping(self, label: int, tokens: List[int]) -> int:
        """Function that remaps IDs of the considered tokens from 0 to len(tokens)"""
        mapping = dict(zip(tokens, range(len(tokens))))
        return mapping[label]

    def _filter_logits(
        self,
        logits: torch.Tensor,
        labels: torch.Tensor,
        tokens: List[int],
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Remove unconsidered tokens from sequences and logits

        Args:
            logits (torch.Tensor): shape -> (num_seqs, max_seq_len, vocab_size)
            labels (torch.Tensor): shape -> (num_seqs, max_seq_len)
            tokens (List[int]): len -> (num_considered_token)

        Returns:
            logits (torch.Tensor): shape -> (sum_considered_token, num_considered_token)
            labels (torch.Tensor): shape -> (sum_considered_token,)
        """
        mask_filter = torch.zeros(labels.shape, dtype=torch.bool)
        for token_id in tokens:
            mask_filter += labels == token_id
        return (
            logits[mask_filter][:, tokens],
            self._labels_remapping(labels[mask_filter], tokens),
        )

    def _filter_loglikelihoods(
        self,
        logits: torch.Tensor,
        labels: torch.Tensor,
        tokens: List[int],
    ) -> torch.Tensor:

        masks = torch.zeros(labels.shape, dtype=torch.bool)
        for token_id in tokens:
            masks += labels == token_id

        loglikelihoods = []
        log_softmax = torch.nn.LogSoftmax(dim=0)
        # loop over the sequences
        for sequence_logit, sequence_label, sequence_mask in zip(logits, labels, masks):
            if sum(sequence_mask) == 0:
                loglikelihood = torch.tensor(float("NaN"))
            else:
                loglikelihood = 0
                # loop over the tokens
                for logit, label, mask in zip(
                    sequence_logit, sequence_label, sequence_mask
                ):
                    if mask:
                        loglikelihood += log_softmax(logit[tokens])[
                            self._label_remapping(label.item(), tokens)
                        ]
            loglikelihoods.append(loglikelihood)
        return torch.stack(loglikelihoods)

    def _filter_and_pool_embeddings(
        self,
        embeddings: torch.Tensor,
        labels: torch.Tensor,
        tokens: List[int],
        pool_mode: Tuple[str, ...] = ("cls", "mean"),
    ) -> Dict[str, torch.Tensor]:
        """Remove unconsidered tokens from sequences and pool embeddings

        Args:
            logits (torch.Tensor): shape -> (num_seqs, max_seq_len, vocab_size)
            labels (torch.Tensor): shape -> (num_seqs, max_seq_len)
            tokens (List[int]): len -> (num_considered_token)
            pool_mode (Tuple[str]):

        Returns:
            embeddings[str] (torch.Tensor): shape -> (num_seqs, emb_size)
        """
        # cls pooling
        embeddings_dict = {}
        if "cls" in pool_mode:
            embeddings_dict["cls"] = embeddings[:, 0, :]

        # tokens filtering
        mask_filter = torch.zeros(labels.shape, dtype=torch.bool)
        for token_id in tokens:
            mask_filter += labels == token_id
        embeddings = [seq[msk] for seq, msk in zip(embeddings, mask_filter)]

        # embeddings pooling
        if "mean" in pool_mode:
            embeddings_dict["mean"] = torch.stack(
                [torch.mean(emb.float(), axis=0) for emb in embeddings]
            )
        if "max" in pool_mode:
            embeddings_dict["max"] = torch.stack(
                [torch.max(emb.float(), 0)[0] for emb in embeddings]
            )
        if "min" in pool_mode:
            embeddings_dict["min"] = torch.stack(
                [torch.min(emb.float(), 0)[0] for emb in embeddings]
            )

        return embeddings_dict

    def _model_evaluation(
        self,
        model_inputs: Dict[str, torch.tensor],
        batch_size: int = 1,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Compute logits and embeddings

        Function which computes logits and embeddings based on a list of sequences,
        a provided batch size and an inference configuration. The output is obtained
        by computing a forward pass through the model ("forward inference")

        Args:
            model_inputs (Dict[str, torch.tensor]): [description]
            batch_size (int): [description]

        Returns:
            Tuple[torch.tensor, torch.tensor]:
                    * logits [num_seqs, max_len_seqs, vocab_size]
                    * embeddings [num_seqs, max_len_seqs+1, embedding_size]
        """

        # Initialize logits and embeddings before looping over batches
        logits = torch.Tensor()  # [num_seqs, max_len_seqs+1, vocab_size]
        embeddings = torch.Tensor()  # [num_seqs, max_len_seqs+1, embedding_size]

        for batch_inputs in tqdm(
            self._generate_chunks(model_inputs, batch_size),
            total=self._get_num_batch_iter(model_inputs, batch_size),
        ):
            batch_logits, batch_embeddings = self._model_pass(batch_inputs)

            embeddings = torch.cat((embeddings, batch_embeddings), dim=0)
            logits = torch.cat((logits, batch_logits), dim=0)

        return logits, embeddings

    def _compute_logits(
        self, model_inputs: Dict[str, torch.Tensor], batch_size: int, pass_mode: str
    ) -> torch.Tensor:
        """Intermediate function to compute logits

        Args:
            model_inputs[str] (torch.Tensor): shape -> (num_seqs, max_seq_len)
            batch_size (int)
            pass_mode (str)

        Returns:
            logits (torch.Tensor): shape -> (num_seqs, max_seq_len, vocab_size)
        """
        if pass_mode == "masked":
            model_inputs, masked_ids_list = self._repeat_and_mask_inputs(model_inputs)
            logits, _ = self._model_evaluation(model_inputs, batch_size=batch_size)
            logits = self._gather_masked_outputs(logits, masked_ids_list)
        elif pass_mode == "forward":
            logits, _ = self._model_evaluation(model_inputs, batch_size=batch_size)
        return logits

    def _compute_accuracy(self, logits: torch.Tensor, labels: torch.Tensor) -> float:
        """Intermediate function to compute accuracy

        Args:
            logits (torch.Tensor): shape -> (sum_considered_token, num_considered_token)
            labels (torch.Tensor): shape -> (sum_considered_token)

        Returns:
            accuracy (float)
        """
        softmaxes = F.softmax(logits, dim=1)
        _, predictions = torch.max(softmaxes, 1)
        accuracies = predictions.eq(labels)

        return accuracies.float().mean().item()

    def _compute_calibration(
        self, logits: torch.Tensor, labels: torch.Tensor, n_bins: int = 10
    ) -> Dict[str, Any]:
        """Intermediate function to compute calibration

        Args:
            logits (torch.Tensor): shape -> (sum_considered_token, num_considered_token)
            labels (torch.Tensor): shape -> (sum_considered_token)
            n_bins (int)

        Returns:
            accuracy (float)
            ece (float)
            reliability_diagram (List[float])
        """
        softmaxes = F.softmax(logits, dim=1)
        confidences, predictions = torch.max(softmaxes, 1)
        accuracies = predictions.eq(labels)

        bin_boundaries = torch.linspace(0, 1, n_bins + 1)
        bin_lowers = bin_boundaries[:-1]
        bin_uppers = bin_boundaries[1:]

        reliability_diagram = []
        ece = torch.zeros(1, device=logits.device)
        for bin_lower, bin_upper in zip(bin_lowers, bin_uppers):
            # Calculated |confidence - accuracy| in each bin
            in_bin = confidences.gt(bin_lower.item()) * confidences.le(bin_upper.item())
            prop_in_bin = in_bin.float().mean()
            if prop_in_bin.item() > 0:
                accuracy_in_bin = accuracies[in_bin].float().mean()
                avg_confidence_in_bin = confidences[in_bin].mean()
                reliability_diagram.append(accuracy_in_bin.item())
                ece += torch.abs(avg_confidence_in_bin - accuracy_in_bin) * prop_in_bin
            else:
                reliability_diagram.append(0.0)

        return {
            "accuracy": accuracies.float().mean().item(),
            "ece": ece.item(),
            "reliability_diagram": reliability_diagram,
        }

    def compute_logits(
        self,
        sequences: Union[List[str], str],
        batch_size: int = 1,
        tokens_list: List[str] = None,
        pass_mode: str = "forward",
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Function that computes the logits from sequences.

        Args:
            sequences_list: List of sequences
            batch_size: Batch size
            pass_mode: Mode of model evaluation ('forward' or 'masked')
            tokens_list: List of tokens to consider

        Returns:
            Tuple[torch.tensor, torch.tensor]: logits and labels in torch.tensor format
        """
        if tokens_list is None:
            tokens_list = NATURAL_AAS_LIST

        if isinstance(sequences, str):
            sequences = load_fasta(sequences)

        _check_sequence(sequences, self.model_dir, 1024)
        _check_memory_logits(sequences, self.vocab_size, pass_mode)

        inputs, labels, tokens = self._process_sequences_and_tokens(
            sequences, tokens_list
        )
        logits = self._compute_logits(inputs, batch_size, pass_mode)
        logits, labels = self._filter_logits(logits, labels, tokens)

        return logits.numpy(), labels.numpy()

    def compute_loglikelihood(
        self,
        sequences: Union[List[str], str],
        batch_size: int = 1,
        tokens_list: List[str] = None,
        pass_mode: str = "forward",
    ) -> np.ndarray:
        """Function that computes loglikelihoods of sequences

        Args:
            sequences: List of sequences
            batch_size: Batch size
            pass_mode: Mode of model evaluation ('forward' or 'masked')
            tokens_list: List of tokens to consider

        Returns:
            torch.Tensor: loglikelihoods in torch.tensor format
        """
        if tokens_list is None:
            tokens_list = NATURAL_AAS_LIST

        if isinstance(sequences, str):
            sequences = load_fasta(sequences)

        _check_sequence(sequences, self.model_dir, 1024)
        _check_memory_logits(sequences, self.vocab_size, pass_mode)

        inputs, labels, tokens = self._process_sequences_and_tokens(
            sequences, tokens_list
        )
        logits = self._compute_logits(inputs, batch_size, pass_mode)
        loglikelihoods = self._filter_loglikelihoods(logits, labels, tokens)

        return loglikelihoods.numpy()

    def compute_embeddings(
        self,
        sequences: Union[List[str], str],
        batch_size: int = 1,
        pool_mode: Tuple[str, ...] = ("cls", "mean"),
        tokens_list: List[str] = None,
    ) -> Dict[str, np.ndarray]:
        """Function that computes embeddings of sequences

        Args:
            sequences: List of sequences or path of fasta file
            batch_size: Batch size
            pool_mode: Mode of pooling ('cls', 'mean', etc...)
            tokens_list: List of tokens to consider

        Returns:
            torch.Tensor: Tensor of shape [number_of_sequences, embeddings_size]
        """
        if tokens_list is None:
            tokens_list = NATURAL_AAS_LIST

        if isinstance(sequences, str):
            sequences = load_fasta(sequences)

        _check_sequence(sequences, self.model_dir, 1024)
        _check_memory_embeddings(sequences, self.embeddings_size, pool_mode)

        inputs, _, tokens = self._process_sequences_and_tokens(sequences, tokens_list)
        embeddings_dict = dict(zip(pool_mode, [torch.Tensor()] * len(pool_mode)))

        for batch_inputs in tqdm(
            self._generate_chunks(inputs, batch_size),
            total=self._get_num_batch_iter(inputs, batch_size),
        ):
            _, batch_embeddings = self._model_pass(batch_inputs)
            batch_labels = batch_inputs["input_ids"]

            batch_embeddings_dict = self._filter_and_pool_embeddings(
                batch_embeddings, batch_labels, tokens, pool_mode
            )

            for key in pool_mode:
                embeddings_dict[key] = torch.cat(
                    (embeddings_dict[key], batch_embeddings_dict[key]), dim=0
                )

        return {key: value.numpy() for key, value in embeddings_dict.items()}

    def compute_accuracy(
        self,
        sequences: Union[List[str], str],
        batch_size: int = 1,
        pass_mode: str = "forward",
        tokens_list: List[str] = None,
    ) -> float:
        """Compute model accuracy from the input sequences

        Args:
            sequences (Union[List[str],str]): list of sequence or fasta file
            batch_size ([type], optional): [description]. Defaults to 1.
            pass_mode ([type], optional): [description]. Defaults to "forward".
            tokens_list ([type], optional): [description]. Defaults to None.

        Returns:
            [type]: [description]
        """
        if tokens_list is None:
            tokens_list = NATURAL_AAS_LIST

        if isinstance(sequences, str):
            sequences = load_fasta(sequences)
        _check_sequence(sequences, self.model_dir, 1024)

        inputs, labels, tokens = self._process_sequences_and_tokens(
            sequences, tokens_list
        )
        logits = self._compute_logits(inputs, batch_size, pass_mode)
        logits, labels = self._filter_logits(logits, labels, tokens)
        accuracy = self._compute_accuracy(logits, labels)

        return accuracy

    def compute_calibration(
        self,
        sequences: Union[List[str], str],
        batch_size: int = 1,
        pass_mode: str = "forward",
        tokens_list: List[str] = None,
        n_bins: int = 10,
    ) -> Dict[str, Any]:
        """Compute model calibration from the input sequences

        Args:
            sequences (Union[List[str],str]): Either a list of sequences of a fasta file
            batch_size ([type], optional): [description]. Defaults to 1.
            pass_mode ([type], optional): [description]. Defaults to "forward".
            tokens_list ([type], optional): [description]. Defaults to None.
            n_bins ([type], optional): [description]. Defaults to 10.

        Returns:
            [type]: [description]
        """
        if tokens_list is None:
            tokens_list = NATURAL_AAS_LIST

        if isinstance(sequences, str):
            sequences = load_fasta(sequences)

        _check_sequence(sequences, self.model_dir, 1024)

        inputs, labels, tokens = self._process_sequences_and_tokens(
            sequences, tokens_list
        )
        logits = self._compute_logits(inputs, batch_size, pass_mode)
        logits, labels = self._filter_logits(logits, labels, tokens)
        calibration_dict = self._compute_calibration(logits, labels, n_bins)

        return calibration_dict

    def load_model(self, model_dir: str, map_location=None):
        """Load state_dict a finetune pytorch model ro a checkpoint directory

        More informations about how to load a model with map_location:
            https://pytorch.org/tutorials/beginner/saving_loading_models.html#saving-loading-model-for-inference

        Args:
            model_dir: path file of the pt model or checkpoint.
        """
        if not os.path.isfile(model_dir):
            raise FileNotFoundError

        if model_dir.endswith(".pt"):
            load_model = torch.load(model_dir)
        elif model_dir.endswith(".ckpt"):
            load_model = torch.load(model_dir)["state_dict"]
        else:
            raise ValueError("Expecting a .pt or .ckpt file")

        if self.multi_gpu:
            self.model.module.load_state_dict(load_model, map_location)  # type: ignore
        else:
            self.model.load_state_dict(load_model, map_location)  # type: ignore

    def save_model(self, exp_path: str, lightning_model: pl.LightningModule) -> str:
        """Save pytorch model in logs directory

        Args:
            exp_path (str): path of the experiments directory in the logs
        """
        version = get_logs_version(exp_path)
        save_name = os.path.join(exp_path, version, self.model_dir + "_finetuned.pt")
        torch.save(lightning_model.model.state_dict(), save_name)
        return save_name
