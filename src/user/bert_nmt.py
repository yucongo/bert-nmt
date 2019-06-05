import argparse
import os.path

import torch

import fairseq.tokenizer
from fairseq import options, utils
from fairseq.models import (
    transformer, register_model, register_model_architecture,
    FairseqModel, FairseqEncoder
)
from fairseq.data import (
    data_utils, Dictionary
)
from fairseq.tasks import (
    register_task, translation
)
from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.modeling import BertModel


class BertCompatibleDictionary(Dictionary):
    def __init__(self, pad='<pad>', eos='</s>', unk='<unk>'):
        # Parent constructor is omitted intentionally.
        #super().__init__()

        self.unk_word, self.pad_word, self.eos_word = unk, pad, eos
        self.symbols = []
        self.count = []
        self.indices = {}
        self.pad_index = self.add_symbol(pad)
        for i in range(1, 100):
            self.add_symbol('[unused%d]' % i)
        self.unk_index = self.add_symbol(unk)
        self.add_symbol('<bos>')
        self.eos_index = self.add_symbol(eos)
        self.nspecial = len(self.symbols)


class BertBasedDictionary:
    def __init__(self, name: str):
        self.__tokenizer = self.__create_tokenizer(name)
        self.__pad, self.__unk, self.__bos, self.__eos = \
            self.__tokenizer.convert_tokens_to_ids(['[PAD]', '[UNK]', '[CLS]', '[SEP]'])
        self.unk_word = '<unk>'

    def __len__(self):
        return len(self.__tokenizer.vocab)

    def __create_tokenizer(self, name: str):
        suffix = (
            'st', 'nd', 'rd', 'th',
            'mm', 'm',
            'ns', 'ms', 's', 'min', 'hr', 'h'
        )
        never_split = ['[PAD]', '[UNK]', '[CLS]', '[SEP]']
        for s in suffix:
            never_split.append('##' + s)
        for i in range(10):
            never_split.append('##' + str(i))

        lower = (name == 'bert-base-chinese' or name.find('uncased') >= 0)

        return BertTokenizer.from_pretrained(name, never_split=never_split, do_lower_case=lower)

    def dummy_sentence(self, length):
        t = torch.Tensor(length).uniform_(self.eos() + 2, len(self)).long()
        t[-1] = self.eos()
        return t

    def unk_string(self, escape=False):
        """Return unknown string, optionally escaped as: <<unk>>"""
        if escape:
            return '<{}>'.format(self.unk_word)
        else:
            return self.unk_word

    def string(self, tensor, bpe_symbol=None, escape_unk=False):
        """Helper for converting a tensor of token indices to a string.

        Can optionally remove BPE symbols or escape <unk> words.
        """
        if torch.is_tensor(tensor) and tensor.dim() == 2:
            return '\n'.join(self.string(t) for t in tensor)

        tk = self.__tokenizer.convert_ids_to_tokens([x.item() for x in tensor])
        return ' '.join(tk)

    def encode_line(self, line, reverse_order=False, **kwargs):
        words = self.__tokenizer.tokenize(line)
        if len(words) > 512:
            print(line)
        if reverse_order:
            words = list(reversed(words))
        words.insert(0, '[CLS]')
        words.append('[SEP]')
        ids = self.__tokenizer.convert_tokens_to_ids(words)
        return torch.IntTensor(ids)

    def pad(self):
        return self.__pad

    def unk(self):
        return self.__unk

    def bos(self):
        return self.__bos

    def eos(self):
        return self.__eos


class BertTranslationEncoder(FairseqEncoder):
    def __init__(self, args: argparse.Namespace, dictionary: BertBasedDictionary=None):
        if dictionary is None:
            dictionary = BertBasedDictionary(args.bert_name)
        super().__init__(dictionary)
        self.args = args
        self.bert = BertModel.from_pretrained(args.bert_name)
        if not args.no_freeze_bert:
            for p in self.bert.parameters():
                p.requires_grad = False

    def forward(self, src_tokens, src_lengths):
        paddings = src_tokens.eq(self.dictionary.pad())
        masks = paddings ^ 1
        if not paddings.any():
            paddings = None

        bert_out = self.bert(src_tokens, torch.zeros_like(src_tokens),
            masks)
        encoder_out = bert_out[0][self.args.bert_layer].transpose(0, 1)

        return encoder_out, paddings

    def reorder_encoder_out(self, encoder_out, new_order):
        tmp = {
            'encoder_out': encoder_out[0],
            'encoder_padding_mask': encoder_out[1]
        }
        transformer.TransformerEncoder.reorder_encoder_out(self,
            tmp, new_order)
        return tmp['encoder_out'], tmp['encoder_padding_mask']

    def max_positions(self) -> int:
        return 512


@register_model('bert_nmt')
class BertTranslationModel(FairseqModel):
    @staticmethod
    def add_args(parser: argparse.ArgumentParser):
        parser.add_argument('--bert-layer', type=int)
        parser.add_argument('--no-freeze-bert', default=False, action='store_true')

    @classmethod
    def build_model(cls, args: argparse.Namespace,
            task: translation.FairseqTask):
        encoder = BertTranslationEncoder(args, task.source_dictionary)
        decoder = cls.__build_transformer_decoder(args, task.target_dictionary)
        return BertTranslationModel(encoder, decoder)

    def forward(self, src_tokens, src_lengths, prev_output_tokens):
        encoder_out = self.encoder(src_tokens, src_lengths)
        dict_out = {
            'encoder_out': encoder_out[0],
            'encoder_padding_mask': encoder_out[1]
        }
        decoder_out = self.decoder(prev_output_tokens, dict_out)
        return decoder_out

    @classmethod
    def __build_transformer_decoder(cls, args: argparse.Namespace,
            tgt_dict: Dictionary):
        decoder_embed_tokens = cls.__build_embedding(tgt_dict,
            args.decoder_embed_dim)
        decoder = transformer.TransformerDecoder(args, tgt_dict,
            decoder_embed_tokens)
        return decoder

    @staticmethod
    def __build_embedding(dictionary, embed_dim):
        num_embeddings = len(dictionary)
        padding_idx = dictionary.pad()
        emb = transformer.Embedding(num_embeddings, embed_dim, padding_idx)
        return emb


@register_task('bert_translation')
class BertTranslationTask(translation.TranslationTask):
    @staticmethod
    def add_args(parser: argparse.ArgumentParser):
        """Add task-specific arguments to the parser."""
        # fmt: off
        parser.add_argument('--bert-name',
                            choices=('bert-base-cased', 'bert-base-uncased',
                                     'bert-large-cased', 'bert-large-uncased',
                                     'bert-base-multilingual-uncased',
                                     'bert-base-multilingual-cased',
                                     'bert-base-chinese'),
                            help='pretrained bert model name')
        translation.TranslationTask.add_args(parser)
        # fmt: on

    @staticmethod
    def load_pretrained_model(path, src_dict_path, tgt_dict_path, arg_overrides=None):
        model = utils.load_checkpoint_to_cpu(path)
        args = model['args']
        state_dict = model['model']
        args = utils.override_model_args(args, arg_overrides)
        src_dict = BertBasedDictionary(args.bert_name)
        tgt_dict = Dictionary.load(tgt_dict_path)
        assert src_dict.pad() == tgt_dict.pad()
        assert src_dict.eos() == tgt_dict.eos()
        assert src_dict.unk() == tgt_dict.unk()

        task = BertTranslationTask(args, src_dict, tgt_dict)
        model = task.build_model(args)
        model.upgrade_state_dict(state_dict)
        model.load_state_dict(state_dict, strict=True)
        return model

    @classmethod
    def setup_task(cls, args, **kwargs):
        """Setup the task (e.g., load dictionaries).

        Args:
            args (argparse.Namespace): parsed command-line arguments
        """
        args.left_pad_source = options.eval_bool(args.left_pad_source)
        args.left_pad_target = options.eval_bool(args.left_pad_target)

        # find language pair automatically
        if args.source_lang is None or args.target_lang is None:
            args.source_lang, args.target_lang = data_utils.infer_language_pair(args.data[0])
        if args.source_lang is None or args.target_lang is None:
            raise Exception('Could not infer language pair, please provide it explicitly')

        src_dict = BertBasedDictionary(args.bert_name)
        tgt_dict = cls.load_dictionary(os.path.join(args.data[0], 'dict.{}.txt'.format(args.target_lang)))
        assert src_dict.pad() == tgt_dict.pad(), "%d != %d" % (src_dict.pad(), tgt_dict.pad())
        assert src_dict.eos() == tgt_dict.eos(), "%d != %d" % (src_dict.eos(), tgt_dict.eos())
        assert src_dict.unk() == tgt_dict.unk(), "%d != %d" % (src_dict.unk(), tgt_dict.unk())
        print('| [{}] dictionary: {} types'.format(args.source_lang, len(src_dict)))
        print('| [{}] dictionary: {} types'.format(args.target_lang, len(tgt_dict)))

        return cls(args, src_dict, tgt_dict)

    @classmethod
    def load_dictionary(cls, filename):
        """Load the dictionary from the filename

        Args:
            filename (str): the filename
        """
        return BertCompatibleDictionary.load(filename)

    @classmethod
    def build_dictionary(cls, filenames, workers=1, threshold=-1, nwords=-1, padding_factor=8):
        """Build the dictionary

        Args:
            filenames (list): list of filenames
            workers (int): number of concurrent workers
            threshold (int): defines the minimum word count
            nwords (int): defines the total number of words in the final dictionary,
                including special symbols
            padding_factor (int): can be used to pad the dictionary size to be a
                multiple of 8, which is important on some hardware (e.g., Nvidia
                Tensor Cores).
        """
        d = BertCompatibleDictionary()
        for filename in filenames:
            Dictionary.add_file_to_dictionary(filename, d, fairseq.tokenizer.tokenize_line, workers)
        d.finalize(threshold=threshold, nwords=nwords, padding_factor=padding_factor)
        return d


@register_model_architecture('bert_nmt', 'bert_nmt')
def bert_nmt_base(args: argparse.Namespace):
    args.bert_layer = getattr(args, 'bert_layer', -2)
    args.decoder_embed_path = getattr(args, 'decoder_embed_path', None)
    if args.bert_name.find('base') >= 0:
        args.decoder_embed_dim = 768
    else:
        args.decoder_embed_dim = 1024
    args.decoder_ffn_embed_dim = getattr(args, 'decoder_ffn_embed_dim', 2048)
    args.decoder_layers = getattr(args, 'decoder_layers', 6)
    args.decoder_attention_heads = getattr(args, 'decoder_attention_heads', 8)
    args.decoder_normalize_before = getattr(args, 'decoder_normalize_before', False)
    args.decoder_learned_pos = getattr(args, 'decoder_learned_pos', False)
    args.attention_dropout = getattr(args, 'attention_dropout', 0.)
    args.relu_dropout = getattr(args, 'relu_dropout', 0.)
    args.dropout = getattr(args, 'dropout', 0.1)
    args.adaptive_softmax_cutoff = getattr(args, 'adaptive_softmax_cutoff', None)
    args.adaptive_softmax_dropout = getattr(args, 'adaptive_softmax_dropout', 0)
    args.share_decoder_input_output_embed = getattr(args, 'share_decoder_input_output_embed', False)
    args.share_all_embeddings = getattr(args, 'share_all_embeddings', False)
    args.no_token_positional_embeddings = getattr(args, 'no_token_positional_embeddings', False)
    args.adaptive_input = getattr(args, 'adaptive_input', False)

    args.decoder_output_dim = getattr(args, 'decoder_output_dim', args.decoder_embed_dim)
    args.decoder_input_dim = getattr(args, 'decoder_input_dim', args.decoder_embed_dim)


@register_model_architecture('bert_nmt', 'bert_nmt_big')
def bert_nmt_big(args):
    args.decoder_ffn_embed_dim = getattr(args, 'decoder_ffn_embed_dim', 4096)
    args.decoder_attention_heads = getattr(args, 'decoder_attention_heads', 16)
    args.dropout = getattr(args, 'dropout', 0.3)
    bert_nmt_base(args)


@register_model_architecture('bert_nmt', 'bert_nmt_big_t2t')
def bert_nmt_big_t2t(args: argparse.Namespace):
    args.decoder_normalize_before = getattr(args, 'decoder_normalize_before', True)
    args.attention_dropout = getattr(args, 'attention_dropout', 0.1)
    args.relu_dropout = getattr(args, 'relu_dropout', 0.1)
    bert_nmt_big(args)
