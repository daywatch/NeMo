# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json

import torch
from tqdm import tqdm

from nemo.collections.nlp.modules.common.megatron.utils import build_position_ids
from nemo.core import Dataset
from nemo.utils import logging

__all__ = ['GPTPromptLearningDataset']


class GPTPromptLearningDataset(Dataset):
    """
    The dataset class for prompt-tuning or p-tuning pretrained GPT models.
    """

    def __init__(
        self,
        datasets,
        tokenizer,
        virtual_prompt_source: str,
        task_templates: dict,
        pseudo_tokens,
        pad_token_id: str,
        max_seq_length: int,
        min_seq_length: int = 1,
        add_bos: bool = False,
        add_eos: bool = True,
    ):
        self.tokenizer = tokenizer
        self.virtual_prompt_source = virtual_prompt_source
        self.task_templates = task_templates
        self.pseudo_tokens = pseudo_tokens
        self.pseudo_token_ids = set(self.tokenizer.tokens_to_ids(self.pseudo_tokens))
        self.pad_token_id = pad_token_id
        self.max_seq_length = max_seq_length
        self.min_seq_length = min_seq_length
        self.add_bos = add_bos
        self.add_eos = add_eos
        self.examples = []

        assert self.min_seq_length <= max_seq_length, "Min sequence length should be less than or equal to max"
        assert self.max_seq_length > 0, "Max sequence length should be greater than 0"

        logging.info("Loading and tokenizing dataset ... ")

        # Datasets is just a list of json dicts
        if isinstance(datasets[0], dict):
            self.load_data(datasets)

        # Datasets are a list of file path strings to .json or .jsonl files
        elif isinstance(datasets[0], str):
            for path in datasets:
                dataset = open(path, 'r', encoding='utf-8')
                self.load_data(dataset)
        else:
            raise ValueError("Datasets must be a list of dicts or a list of filepath strings")

    def load_data(self, dataset):
        """
        Loads a dataset by filling in the task templates specified in the config file
        with the information from each training/inference example. Converts all input 
        text into token ids. Also replaces the <|VIRTUAL_PROMPT_#|> placeholders in 
        the task templates with the actual virtual prompt token ids. 

        params:
            dataset: A list of json objects or a dictionary objects each
                     containing the information needed for a training example
        """
        skipped = 0

        for json_line in tqdm(dataset):

            # Read example dict or load the information for a single example from .json file
            if type(json_line) == dict:
                doc = json_line
            else:
                doc = json.loads(json_line)

            taskname = doc["taskname"]
            prompt_template = self.task_templates[taskname]["prompt_template"]
            prompt_template_fields = self.task_templates[taskname]["prompt_template_fields"]
            total_virtual_tokens = self.task_templates[taskname]["total_virtual_tokens"]
            virtual_token_splits = self.task_templates[taskname]["virtual_token_splits"]
            input_example = prompt_template

            self._input_sanity_checks(
                total_virtual_tokens, virtual_token_splits, prompt_template, prompt_template_fields, doc
            )

            # Format the input example according to the template
            input_example = self._insert_text_in_template(input_example, prompt_template_fields, doc)
            input_example = self._insert_virtual_token_placeholders(input_example, virtual_token_splits)
            input_ids = self.tokenizer.text_to_ids(input_example)

            # Add BOS/EOS if desired, adds EOS by default
            if self.add_bos:
                input_ids = [self.tokenizer.bos_id] + input_ids
            if self.add_eos:
                input_ids = input_ids + [self.tokenizer.eos_id]

            # Try to truncate input text to fit into the max sequence length
            if len(input_ids) > self.max_seq_length:
                input_ids = self._truncate_input(input_ids, taskname, doc)

            # Skip example if the final length doesn't fit length requirements even after truncation
            if self.min_seq_length <= len(input_ids) <= self.max_seq_length:
                if self.virtual_prompt_source == "prompt-encoder":
                    taskname_id = self.tokenizer.text_to_ids(taskname)

                elif self.virtual_prompt_source == "prompt-table":
                    taskname_id = self.task_templates[taskname]["task_id_num"]

                self.examples.append((taskname_id, input_ids))

            else:
                skipped += 1

        logging.info(f'Skipped {skipped} sentences, sequence length too short or too long even after truncation')

    def _input_sanity_checks(
        self, total_virtual_tokens, virtual_token_splits, prompt_template, prompt_template_fields, doc
    ):
        # Sanity check amount of virtual token
        assert total_virtual_tokens > 0, "There should be at least one virtual prompt token"
        assert (
            total_virtual_tokens < self.max_seq_length
        ), "virtual prompt tokens should not exceed max sequence length"

        # Make sure virtual token splits add up to the total number of virtual tokens
        assert (
            sum(virtual_token_splits) == total_virtual_tokens
        ), "Sum of prompt token split values must equal total number of prompt tokens"

        # Make sure number of virtual prompt locations match the number of virtual prompt splits
        assert prompt_template.count('<|VIRTUAL_PROMPT_') == len(
            virtual_token_splits
        ), "The number of '<|VIRTUAL_PROMPT_n|>' markers and the number of prompt token splits must match"

        # Check if input example has fields not present in template
        keys_not_in_template = list(set(doc.keys()) - set(prompt_template_fields) - set(['taskname']))
        assert (
            len(keys_not_in_template) == 0
        ), f"Examples in your dataset contain the fields: {keys_not_in_template} that are not in the task template."

    def _insert_text_in_template(self, input_example, prompt_template_fields, doc):
        """ Format the input example according to the template """
        for field in prompt_template_fields:
            if field in doc.keys():
                field_text = doc[field]
                input_example = input_example.replace('{' + field + '}', field_text)

            # If some fields from the template aren't present, e.g. {answer} during inference
            # just remove that field from the template, leaving the space blank
            else:
                input_example = input_example.replace('{' + field + '}', "")

        return input_example

    def _insert_virtual_token_placeholders(self, input_example, virtual_token_splits):
        """ Insert the correct number of pseudo tokens at the <|virtual_PROMPT_n|> markers """
        total_inserted_tokens = 0

        for idx in range(len(virtual_token_splits)):
            split_start = total_inserted_tokens
            split_end = total_inserted_tokens + virtual_token_splits[idx]
            pseudo_tokens_for_split = "".join(self.pseudo_tokens[split_start:split_end])
            input_example = input_example.replace(f'<|VIRTUAL_PROMPT_{idx}|>', pseudo_tokens_for_split)
            total_inserted_tokens = split_end

        return input_example

    def _truncate_input(self, input_ids, taskname, doc):
        """ Try to truncate input text to fit into the max sequence length """
        truncation_field = self.task_templates[taskname]['truncate_field']
        logging.info(
            f"Input greater than max sequence length. Attempting to truncate: '{truncation_field}' in task: '{taskname}'"
        )

        # Truncate the text ids in this part of input to try and fit max sequence length
        if truncation_field is not None and truncation_field in doc.keys():
            truncation_length = len(input_ids) - self.max_seq_length
            field_text = doc[truncation_field]

            # Add leading space to text if there is a space before it in the template
            prompt_template = self.task_templates[taskname]["prompt_template"]
            field_text_start = prompt_template.find("{" + truncation_field + "}")
            if field_text_start != 0 and prompt_template[field_text_start - 1] == " ":
                field_text = " " + field_text

            # Truncate field text
            field_text_ids = self.tokenizer.text_to_ids(field_text)
            truncated_text_ids = field_text_ids[: -min(truncation_length, len(field_text_ids))]

            # Replace original text ids with truncated text ids
            field_start, field_end = find_subsequence_location(input_ids, field_text_ids)
            input_ids = input_ids[:field_start] + truncated_text_ids + input_ids[field_end + 1 :]

        return input_ids

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]

    def collate_fn(self, batch):
        """ Prepares input_ids, labels, loss mask, attention_mask, and position ids for global batch """
        # Get max sequence length of batch
        taskname_ids, input_ids = zip(*batch)

        # Pad taskname_ids to be the same length for the prompt encoder
        if self.virtual_prompt_source == "prompt-encoder":
            max_taskname_length = max(len(ids) for ids in taskname_ids)
            taskname_ids = [ids + [self.pad_token_id] * (max_taskname_length - len(ids)) for ids in taskname_ids]
            taskname_ids = torch.tensor(taskname_ids)

        # Task ids are just used for a look up embeddings for prompt-table
        elif self.virtual_prompt_source == "prompt-table":
            taskname_ids = torch.tensor(taskname_ids)

        batch_max = max(len(ids) for ids in input_ids)
        input_ids, loss_mask = self.pad_batch_and_build_loss_mask(input_ids, batch_max)

        # Should be a label for every token in batch, label is the next token
        labels = input_ids[:, 1:].contiguous()
        input_ids = input_ids[:, :-1].contiguous()
        batch_max -= 1

        # Loss mask should align with labels
        loss_mask = loss_mask[:, 1:].contiguous()

        # Using causal attention mask for whole input
        batch_size = len(input_ids)
        attention_mask = torch.tril(torch.ones((batch_size, batch_max, batch_max))).view(
            batch_size, 1, batch_max, batch_max
        )

        # Convert attention mask from float to bool
        attention_mask = attention_mask < 0.5
        position_ids = build_position_ids(input_ids)

        return input_ids, labels, loss_mask, position_ids, attention_mask, taskname_ids

    def pad_batch_and_build_loss_mask(self, input_ids, batch_max):
        """ Pad input_ids in batch to max batch length while building loss mask """
        batch_loss_masks = []
        for ids in input_ids:
            # Loss mask where virtual tokens are 0.0 and all other tokens are 1.0
            loss_mask = [float(token_id not in self.pseudo_token_ids) for token_id in ids]

            # Pad to max length
            input_length = len(ids)
            padding_length = batch_max - input_length
            ids.extend([self.pad_token_id] * padding_length)

            # Account for padding in loss mask
            loss_mask.extend([0.0] * padding_length)
            batch_loss_masks.append(torch.tensor(loss_mask, dtype=torch.float))

        # Make into torch tensors
        input_ids = torch.tensor(input_ids, dtype=torch.long)
        batch_loss_masks = torch.stack(batch_loss_masks)

        return input_ids, batch_loss_masks

    def get_all_examples(self, tokens_to_generate):
        """
        Used for loading inference data. 
        """
        task_id_nums, input_ids = zip(*self.examples)
        input_lengths = torch.cuda.LongTensor([len(inputs) for inputs in input_ids])
        task_id_nums = torch.cuda.LongTensor(task_id_nums)
        batch_max = input_lengths.max().item()

        input_ids, _ = self.pad_batch_and_build_loss_mask(input_ids, batch_max + tokens_to_generate)
        input_ids = input_ids.cuda()
        input_ids = torch.cuda.LongTensor(input_ids)

        return task_id_nums, (input_ids, input_lengths)


def find_subsequence_location(sequence, subsequence):
    """ Finds the start and end index of the first occurance 
        of a given subsequence within a larger list. Returns 
        the two indices corresponding to the postition of 
        the first and last token of the subseqeunce.
        Assumes subsequence is known to be in sequence. 
    """
    assert len(sequence) >= len(subsequence), "subsequence too long"

    start_idx = None
    next_subseq_token = subsequence[0]
    next_subsequence_idx = 1

    for seq_idx, token in enumerate(sequence):
        if token == next_subseq_token:
            if start_idx is None:
                start_idx = seq_idx

            if next_subsequence_idx == len(subsequence):
                end_idx = seq_idx
                return start_idx, end_idx
            else:
                next_subseq_token = subsequence[next_subsequence_idx]
                next_subsequence_idx += 1
        else:
            start_idx = None
            next_subseq_token = subsequence[0]
            next_subsequence_idx = 1

    raise ValueError("Subsequence not found in sequence")
