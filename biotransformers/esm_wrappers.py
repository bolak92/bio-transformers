"""
This script defines a class which inherits from the TransformersWrapper class, and is
specific to the ESM model developed by FAIR (https://github.com/facebookresearch/esm).
"""
from typing import Dict, List, Tuple

import esm
import numpy as np
import torch
from tqdm import tqdm
from torch.nn import DataParallel

from .transformers_wrappers import (
    TransformersModelProperties,
    TransformersWrapper,
)

from .gpus_utils import set_device

# List all ESM models
esm_list = [
    # "esm1_t34_670M_UR50S",
    # "esm1_t34_670M_UR50D",
    "esm1_t34_670M_UR100",
    # "esm1_t12_85M_UR50S",
    "esm1_t6_43M_UR50S",
    "esm1b_t33_650M_UR50S",
    "esm_msa1_t12_100M_UR50S",
]

# Define a default ESM model
DEFAULT_MODEL = "esm1_t34_670M_UR100"


class ESMWrapper(TransformersWrapper):
    """
    Class that uses an ESM type of pretrained transformers model to evaluate
    a protein likelihood so as other insights.
    """

    def __init__(self, model_dir: str, device, multi_gpu):

        if model_dir not in esm_list:
            print(
                f"Model dir '{model_dir}' not recognized. "
                f"Using '{DEFAULT_MODEL}' as default"
            )
            model_dir = DEFAULT_MODEL

        super().__init__(model_dir, _device=device, multi_gpu=multi_gpu)

        self.model, self.alphabet = esm.pretrained.load_model_and_alphabet(model_dir)
        self.num_layers = self.model.num_layers

        # TODO: use nn.Parallel to make parallel inference
        if self.multi_gpu:
            self.model = DataParallel(self.model).to(self._device)
        else:
            self.model = self.model.to(self._device)
        self.batch_converter = self.alphabet.get_batch_converter()

    @property
    def clean_model_id(self) -> str:
        """Clean model ID (in case the model directory is not)"""
        return self.model_id

    @property
    def model_property(self) -> TransformersModelProperties:
        """Returns a class with model properties"""
        return TransformersModelProperties(
            num_sep_tokens=1, begin_token=True, end_token=False
        )

    @property
    def model_vocab_tokens(self) -> List[str]:
        """List of all vocabulary tokens to consider (as strings), which may be a subset
        of the model vocabulary (based on self.vocab_token_list)"""
        voc = (
            self.vocab_token_list
            if self.vocab_token_list is not None
            else self.alphabet.all_toks
        )
        return voc

    @property
    def model_vocabulary(self) -> List[str]:
        """Returns the whole vocabulary list"""
        return list(self.alphabet.tok_to_idx.keys())

    @property
    def model_vocab_ids(self) -> List[int]:
        """List of all vocabulary IDs to consider (as ints), which may be a subset
        of the model vocabulary (based on self.vocab_token_list)"""
        return [self.token_to_id(tok) for tok in self.model_vocab_tokens]

    @property
    def mask_token(self) -> str:
        """Representation of the mask token (as a string)"""
        return self.alphabet.all_toks[self.alphabet.mask_idx]  # "<mask>"

    @property
    def pad_token(self) -> str:
        """Representation of the pad token (as a string)"""
        return self.alphabet.all_toks[self.alphabet.padding_idx]  # "<pad>"

    @property
    def begin_token(self) -> str:
        """Representation of the beginning of sentence token (as a string)"""
        return "<cls>"

    @property
    def end_token(self) -> str:
        """Representation of the end of sentence token (as a string). This token doesn't
        exist in the case of ESM, thus we return an empty string."""
        return ""

    @property
    def token_to_id(self):
        """Returns a function which maps tokens to IDs"""
        return lambda x: self.alphabet.tok_to_idx[x]

    def _process_sequences_and_tokens(
        self, sequences_list: List[str], tokens_list: List[str]
    ) -> Tuple[Dict[str, torch.tensor], torch.tensor, List[int]]:
        """Function to transform tokens string to IDs; it depends on the model used"""
        tokens = []
        for token in tokens_list:
            if token not in self.model_vocabulary:
                print("Warnings; token", token, "does not belong to model vocabulary")
            else:
                tokens.append(self.token_to_id(token))

        _, _, all_tokens = self.batch_converter(
            [("", sequence) for sequence in sequences_list]
        )
        encoded_inputs = {
            "input_ids": all_tokens,
            "attention_mask": 1 * (all_tokens != self.token_to_id(self.pad_token)),
            "token_type_ids": torch.zeros(all_tokens.shape),
        }
        return encoded_inputs, all_tokens.to("cpu"), tokens

    def _model_evaluation(
        self, model_inputs: Dict[str, torch.tensor], batch_size: int = 1,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
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
        # Define number of iterations
        num_batch_iter = int(np.ceil(model_inputs["input_ids"].shape[0] / batch_size))
        # Initialize probabilities and embeddings before looping over batches
        logits = torch.Tensor()  # [num_seqs, max_len_seqs+1, vocab_size]
        embeddings = torch.Tensor()  # [num_seqs, max_len_seqs+1, embedding_size]

        all_tokens = model_inputs["input_ids"]

        for batch_tokens in tqdm(
            self._generate_chunks(all_tokens, batch_size), total=num_batch_iter
        ):
            batch_tokens = batch_tokens.to(self._device)
            last_layer = self.num_layers - 1

            with torch.no_grad():
                results = self.model(batch_tokens, repr_layers=[last_layer])

            # Also include first token embedding (for the beginning of the sentence)
            new_embeddings = results["representations"][last_layer]
            new_embeddings = new_embeddings.detach().cpu()
            embeddings = torch.cat((embeddings, new_embeddings), dim=0)

            #  Get the logits : token 0 is always a beginning-of-sequence token
            #  , so the first residue is token 1.
            new_logits = results["logits"].detach().cpu()
            logits = torch.cat((logits, new_logits), dim=0)

        return logits, embeddings
