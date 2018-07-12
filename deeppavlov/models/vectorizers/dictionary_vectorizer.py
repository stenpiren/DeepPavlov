import pathlib
from collections import defaultdict
from typing import List, Dict, Generator, Tuple, Any, AnyStr

import numpy as np

from sortedcontainers import SortedListWithKey
from pymorphy2 import MorphAnalyzer
from russian_tagsets import converters

from deeppavlov.core.models.serializable import Serializable
from deeppavlov.core.models.component import Component
from deeppavlov.core.common.registry import register


@register("dictionary_vectorizer")
class DictionaryVectorizer(Serializable):
    """
    Transforms words into 0-1 vector of its possible tags, read from a vocabulary file.
    The format of the vocabulary must be
        word<TAB>tag_1<SPACE>...<SPACE>tag_k

    save_path: str, path to save the vocabulary,
    load_path: str or list of strs, path to the vocabulary(-ies),
    min_freq: int, default=1, minimal frequency of tag to memorize this tag,
    unk_token: str or None, unknown token to be yielded for unknown words
    """
    def __init__(self, save_path, load_path, min_freq=1, unk_token=None, **kwargs):
        super().__init__(save_path, load_path, **kwargs)
        self.min_freq = min_freq
        self.unk_token = unk_token
        self.load()

    @property
    def dim(self):
        return len(self._t2i)

    def save(self):
        save_path = str(self.save_path)
        with open(save_path, "w", encoding="utf8") as fout:
            for word, curr_labels in sorted(self.word_tag_mapping.items()):
                curr_labels = [self._i2t[index] for index in curr_labels]
                curr_labels = [x for x in curr_labels if x != self.unk_token]
                fout.write("{}\t{}".format(word, " ".join(curr_labels)))
        return self

    def load(self):
        if isinstance(self.load_path, str):
            load_path = pathlib.Path(self.load_path)
            if load_path.is_dir():
                load_path = [str(x) for x in load_path.iterdir() if x.is_file()]
            else:
                load_path = [str(load_path)]
        else:
            load_path = [str(x) for x in self.load_path]
        labels_by_words = defaultdict(set)
        for infile in load_path:
            with open(infile, "r", encoding="utf8") as fin:
                for line in fin:
                    line = line.strip()
                    if line.count("\t") != 1:
                        continue
                    word, labels = line.split("\t")
                    labels_by_words[word].update(labels.split())
        self._train(labels_by_words)
        return self

    def _train(self, labels_by_words : Dict):
        self._i2t = [self.unk_token] if self.unk_token is not None else []
        self._t2i = defaultdict(lambda: self.unk_token)
        freq = defaultdict(int)
        for word, labels in labels_by_words.items():
            for label in labels:
                freq[label] += 1
        self._i2t += [label for label, count in freq.items() if count >= self.min_freq]
        for i, label in enumerate(self._i2t):
            self._t2i[label] = i
        if self.unk_token is not None:
            self.word_tag_mapping = defaultdict(lambda: [self.unk_token])
        else:
            self.word_tag_mapping = defaultdict(list)
        for word, labels in labels_by_words.items():
            labels = {self._t2i[label] for label in labels}
            self.word_tag_mapping[word] = [x for x in labels if x is not None]
        return self

    def __call__(self, data: List[List[AnyStr]]):
        max_length = max(len(x) for x in data)
        answer = np.zeros(shape=(len(data), max_length, self.dim), dtype=int)
        for i, sent in enumerate(data):
            for j, word in enumerate(sent):
                answer[i, j][self.word_tag_mapping[word]] = 1
        return answer


@register("pymorphy_vectorizer")
class PymorphyVectorizer(Serializable):
    """
        Transforms russian words into 0-1 vector of its possible Universal Dependencies tags.
        Tags are obtained using Pymorphy analyzer (pymorphy2.readthedocs.io)
        and transformed to UD2.0 format using russian-tagsets library (https://github.com/kmike/russian-tagsets).
        All UD2.0 tags that are compatible with produced tags are memorized.
        The list of possible Universal Dependencies tags is read from a file,
        which contains all the labels that occur in UD2.0 SynTagRus dataset.

        save_path: str, path to save the tags list
            (must be present by Serializable superclass signature),
        load_path: str, path to load the list of tags,
        max_pymorphy_variants: int, default=1,
            maximal number of pymorphy parses to be used. If -1, all parses are used.
        """

    USELESS_KEYS = ["Abbr"]
    VALUE_MAP = {"Ptan": "Plur", "Brev": "Short"}

    def __init__(self, save_path, load_path, max_pymorphy_variants: int = -1, **kwargs):
        super().__init__(save_path, load_path, **kwargs)
        self.max_pymorphy_variants = max_pymorphy_variants
        self.load()
        self.memorized_word_indexes = dict()
        self.memorized_tag_indexes = dict()
        self.analyzer = MorphAnalyzer()
        self.converter = converters.converter('opencorpora-int', 'ud20')

    @property
    def dim(self):
        return len(self._t2i)

    def save(self):
        save_path = str(self.save_path)
        with open(save_path, "r", encoding="utf8") as fout:
            fout.write("\n".join(self._i2t))

    def load(self):
        load_path = str(self.load_path)
        self._i2t = []
        with open(load_path, "r", encoding="utf8") as fin:
            for line in fin:
                line = line.strip()
                if line == "":
                    continue
                self._i2t.append(line)
        self._t2i = {tag: i for i, tag in enumerate(self._i2t)}
        self._make_tag_trie()
        return self

    def _make_tag_trie(self):
        self._nodes = [defaultdict(dict)]
        self._start_nodes_for_pos = dict()
        self._data = [None]
        for tag, code in self._t2i.items():
            if "," in tag:
                pos, tag = tag.split(",", maxsplit=1)
                tag = sorted([tuple(elem.split("=")) for elem in tag.split("|")])
            else:
                pos, tag = tag, []
            start = self._start_nodes_for_pos.get(pos)
            if start is None:
                start = self._start_nodes_for_pos[pos] = len(self._nodes)
                self._nodes.append(defaultdict(dict))
                self._data.append(None)
            for key, value in tag:
                values_dict = self._nodes[start][key]
                child = values_dict.get(value)
                if child is None:
                    child = values_dict[value] = len(self._nodes)
                    self._nodes.append(defaultdict(dict))
                    self._data.append(None)
                start = child
            self._data[start] = code
        return self

    def __call__(self, data: List[List[AnyStr]]):
        max_length = max(len(x) for x in data)
        answer = np.zeros(shape=(len(data), max_length, self.dim), dtype=int)
        for i, sent in enumerate(data):
            for j, word in enumerate(sent):
                answer[i, j][self._get_word_indexes(word)] = 1
        return answer

    def find_compatible(self, tag):
        if " " in tag and "_" not in tag:
            pos, tag = tag.split(" ", maxsplit=1)
            tag = sorted([tuple(elem.split("=")) for elem in tag.split("|")])
        else:
            pos, tag = tag.split()[0], []
        if pos not in self._start_nodes_for_pos:
            return []
        tag = [(key, self.VALUE_MAP.get(value, value)) for key, value in tag
               if key not in self.USELESS_KEYS]
        if len(tag) > 0:
            curr_nodes = [(0, self._start_nodes_for_pos[pos])]
            final_nodes = []
        else:
            final_nodes = [self._start_nodes_for_pos[pos]]
            curr_nodes = []
        while len(curr_nodes) > 0:
            i, node_index = curr_nodes.pop()
            # key, value = tag[i]
            node = self._nodes[node_index]
            if len(node) == 0:
                final_nodes.append(node_index)
            for curr_key, curr_values_dict in node.items():
                curr_i, curr_node_index = i, node_index
                while curr_i < len(tag) and tag[curr_i][0] < curr_key:
                    curr_i += 1
                if curr_i == len(tag):
                    final_nodes.extend(curr_values_dict.values())
                    continue
                key, value = tag[curr_i]
                if curr_key < key:
                    for child in curr_values_dict.values():
                        curr_nodes.append((curr_i, child))
                else:
                    child = curr_values_dict.get(value)
                    if child is not None:
                        if curr_i < len(tag) - 1:
                            curr_nodes.append((curr_i + 1, child))
                        else:
                            final_nodes.append(child)
        answer = []
        while len(final_nodes) > 0:
            index = final_nodes.pop()
            if self._data[index] is not None:
                answer.append(self._data[index])
            for elem in self._nodes[index].values():
                final_nodes.extend(elem.values())
        return answer

    def _get_word_indexes(self, word):
        answer = self.memorized_word_indexes.get(word)
        if answer is None:
            parse = self.analyzer.parse(word)
            if self.max_pymorphy_variants > 0:
                parse = parse[:self.max_pymorphy_variants]
            tag_indexes = set()
            for elem in parse:
                tag_indexes.update(set(self._get_tag_indexes(elem.tag)))
            answer = self.memorized_word_indexes[word] = list(tag_indexes)
        return answer

    def _get_tag_indexes(self, pymorphy_tag):
        answer = self.memorized_tag_indexes.get(pymorphy_tag)
        if answer is None:
            tag = self.converter(str(pymorphy_tag))
            answer = self.memorized_tag_indexes[pymorphy_tag] = self.find_compatible(tag)
        return answer
