# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
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

import random
from typing import Any, Tuple

import numpy as np
import tensorflow as tf

from deeppavlov.core.common.errors import ConfigError
from deeppavlov.core.common.registry import register
from deeppavlov.core.models.tf_model import TFModel
from deeppavlov.models.coreference_resolution import custom_layers
from deeppavlov.models.coreference_resolution.tf_ops import distance_bins, extract_mentions, get_antecedents, spans


@register("coref_model")
class CorefModel(TFModel):
    """
    End-to-end neural model for coreference resolution.
    Class that create model from https://homes.cs.washington.edu/~kentonl/pub/lhlz-emnlp.2017.pdf
    """

    def __init__(self,
                 embedder: Any = None,
                 emb_lowercase: bool = False,
                 emb_format: str = "std_emb",
                 char_vocab: Any = None,
                 char_embedding_size: int = 8,
                 max_mention_width: int = 10,
                 genres: Tuple[str] = ('bc',),
                 learning_rate: float = 0.001,
                 decay_frequency: float = 100,
                 decay_rate: float = 0.999,
                 final_rate: float = 0.0002,
                 optimizer: str = "adam",
                 filter_widths: Tuple[int] = (3, 4, 5),
                 filter_size: int = 50,
                 max_training_sentences: int = 50,
                 max_antecedents: int = 250,
                 mention_ratio: float = 0.4,
                 lstm_size: int = 200,
                 ffnn_size: int = 150,
                 ffnn_depth: int = 2,
                 feature_size: int = 20,
                 use_metadata: bool = True,
                 use_features: bool = True,
                 max_gradient_norm: float = 5.0,
                 dropout_rate: float = 0.2,
                 lexical_dropout_rate: float = 0.5,
                 anaphora: str = "full",
                 model_heads: bool = True,
                 train_on_gold: bool = True,
                 random_seed: int = 42,
                 rep_iter: int = 144,
                 **kwargs):
        # Parameters
        # ---------------------------------------------------------------------------------

        # embeddings
        self.embedder = embedder
        self.emb_lowercase = emb_lowercase
        self.embedding_size = self.embedder.dim
        self.emb_format = emb_format
        if self.emb_format not in ["std_emb", "cached"]:
            raise ConfigError(f"Embedding format must be 'std_emb' or 'cached', but '{self.emb_format}' was found.")
        self.char_dict = char_vocab
        self.char_embedding_size = char_embedding_size

        # Net
        self.lstm_size = lstm_size
        self.ffnn_size = ffnn_size
        self.ffnn_depth = ffnn_depth
        self.filter_size = filter_size
        self.filter_widths = filter_widths
        self.max_mention_width = max_mention_width
        self.feature_size = feature_size
        self.model_heads = model_heads
        self.use_metadata = use_metadata
        self.use_features = use_features
        self.mention_ratio = mention_ratio
        self.max_antecedents = max_antecedents
        self.max_training_sentences = max_training_sentences
        self.genres = genres
        self.anaphora = anaphora
        self.rep_iter = rep_iter

        # dropout
        self.dropout_rate = dropout_rate
        self.lexical_dropout_rate = lexical_dropout_rate

        # train
        self.optimizer = optimizer
        self.learning_rate = learning_rate
        self.decay_rate = decay_rate
        self.decay_frequency = decay_frequency
        self.final_rate = final_rate
        self.max_gradient_norm = max_gradient_norm

        # other
        self.train_on_gold = train_on_gold
        self.random_seed = random_seed
        self.head_scores = None
        self.dropout = None
        self.lexical_dropout = None
        self.tf_loss = None

        # ----------------------------------------------------------------------------------
        # C++ operations
        self.spans = spans
        self.distance_bins = distance_bins
        self.extract_mentions = extract_mentions
        self.get_antecedents = get_antecedents
        # ----------------------------------------------------------------------------------

        self.genres = {g: i for i, g in enumerate(self.genres)}

        input_props = list()
        input_props.append((tf.float64, [None, None, self.embedding_size]))  # Text embeddings.
        input_props.append((tf.int32, [None, None, None]))  # Character indices.
        input_props.append((tf.int32, [None]))  # Text lengths.
        input_props.append((tf.int32, [None]))  # Speaker IDs.
        input_props.append((tf.int32, []))  # Genre.
        input_props.append((tf.bool, []))  # Is training.
        input_props.append((tf.int32, [None]))  # Gold starts.
        input_props.append((tf.int32, [None]))  # Gold ends.
        input_props.append((tf.int32, [None]))  # Cluster ids.

        self.queue_input_tensors = [tf.placeholder(dtype, shape) for dtype, shape in input_props]
        dtypes, shapes = zip(*input_props)
        queue = tf.PaddingFIFOQueue(capacity=1, dtypes=dtypes, shapes=shapes)
        self.enqueue_op = queue.enqueue(self.queue_input_tensors)
        self.input_tensors = queue.dequeue()

        self.predictions, self.loss = self.get_predictions_and_loss(*self.input_tensors)

        self.global_step = tf.Variable(0, name="global_step", trainable=False)
        self.reset_global_step = tf.assign(self.global_step, 0)

        learning_rate = tf.train.exponential_decay(self.learning_rate, self.global_step, self.decay_frequency,
                                                   self.decay_rate, staircase=True)

        learning_rate = tf.cond(learning_rate < self.final_rate,
                                lambda: tf.Variable(self.final_rate, tf.float32),
                                lambda: learning_rate)

        trainable_params = tf.trainable_variables()

        gradients = tf.gradients(self.loss, trainable_params)

        # gradients = [g if g is None else tf.cast(g, tf.float64) for g in gradients]
        # gradients, _ = tf.clip_by_global_norm(gradients, self.max_gradient_norm)

        optimizers = {"adam": tf.train.AdamOptimizer, "sgd": tf.train.GradientDescentOptimizer}
        optimizer = optimizers[self.optimizer](learning_rate)
        self.train_op = optimizer.apply_gradients(zip(gradients, trainable_params), global_step=self.global_step)

        tf.set_random_seed(self.random_seed)
        config = tf.ConfigProto()
        config.gpu_options.per_process_gpu_memory_fraction = 0.95  # 1.0

        self.sess = tf.Session(config=config)
        self.sess.run(tf.global_variables_initializer())

        super().__init__(**kwargs)
        self.load()

    def start_enqueue_thread(self, train_example, is_training, returning=False):
        """
        Initialize queue of tensors that feed one at the input of the model.
        Args:
            train_example: modified dict from agent
            is_training: training flag
            returning: returning flag

        Returns:
            if returning is True, return list of variables:
                [word_emb, char_index, text_len, speaker_ids, genre, is_training, gold_starts, gold_ends, cluster_ids]
        """
        tensorized_example = self.tensorize_example(train_example, is_training=is_training)
        feed_dict = dict(zip(self.queue_input_tensors, tensorized_example))
        self.sess.run(self.enqueue_op, feed_dict=feed_dict)
        if returning:
            return tensorized_example

    @staticmethod
    def tensorize_mentions(mentions):
        """
        Create two np.array of starts end ends positions of gold mentions.
        Args:
            mentions: list of tuple

        Returns:
            np.array(starts positions), np.array(ends positions)

        """
        if len(mentions) > 0:
            starts, ends = zip(*mentions)
        else:
            starts, ends = [], []
        return np.array(starts), np.array(ends)

    def tensorize_example(self, example, is_training):
        """
        Takes a dictionary from the observation and transforms it into a set of tensors
        for tensorflow placeholders.
        Args:
            example: dict from observation
            is_training: True or False value, use as a returned parameter or flag

        Returns: word_emb, char_index, text_len, speaker_ids, genre, is_training, gold_starts, gold_ends, cluster_ids;
            it numpy tensors for placeholders (is_training - bool)
            If length of the longest sentence in the document is greater than parameter "max_training_sentences",
            the returning method calls the 'truncate_example' function.
        """
        if isinstance(example["clusters"], tuple):
            clusters = example["clusters"][0]
        else:
            clusters = example["clusters"]

        gold_mentions = sorted(tuple(m) for m in custom_layers.flatten(clusters))
        gold_mention_map = {m: i for i, m in enumerate(gold_mentions)}
        cluster_ids = np.zeros(len(gold_mentions))

        for cluster_id, cluster in enumerate(clusters):
            for mention in cluster:
                cluster_ids[gold_mention_map[tuple(mention)]] = cluster_id

        sentences = example["sentences"][0]
        num_words = sum(len(s) for s in sentences)
        speakers = custom_layers.flatten(example["speakers"][0])

        assert num_words == len(speakers)

        max_sentence_length = max(len(s) for s in sentences)
        max_word_length = max(max(max(len(w) for w in s) for s in sentences), max(self.filter_widths))
        char_index = np.zeros([len(sentences), max_sentence_length, max_word_length])
        text_len = np.array([len(s) for s in sentences])
        doc_key = example["doc_key"][0]

        if self.emb_lowercase:
            for i, sentence in enumerate(sentences):
                for j, word in enumerate(sentence):
                    sentences[i][j] = word.lower()

        for i, sentence in enumerate(sentences):
            for j, word in enumerate(sentence):
                char_index[i, j, :len(word)] = [self.char_dict[c] for c in word]

        if self.emb_format == "std_emb":
            word_emb = self.embedder(sentences)
        else:
            word_emb = self.embedder(doc_key)

        speaker_dict = {s: i for i, s in enumerate(set(speakers))}
        speaker_ids = np.array([speaker_dict[s] for s in speakers])  # numpy

        genre = self.genres[doc_key[:2]]  # int 1

        gold_starts, gold_ends = self.tensorize_mentions(gold_mentions)  # numpy of unicode str

        if is_training and len(sentences) > self.max_training_sentences:
            return self.truncate_example(word_emb, char_index, text_len, speaker_ids, genre, is_training, gold_starts,
                                         gold_ends, cluster_ids)
        else:
            return word_emb, char_index, text_len, speaker_ids, genre, is_training, gold_starts, gold_ends, cluster_ids

    def truncate_example(self, word_emb, char_index, text_len, speaker_ids, genre, is_training, gold_starts, gold_ends,
                         cluster_ids):
        """
        It takes the output of the function "tensorize_example" and cuts off the excess part of the tensor.

        Args:
            word_emb: [Amount of sentences, Amount of words in sentence (max len), self.embedding_size],
                float64, Text embeddings.
            char_index: [Amount of words, Amount of chars in word (max len), char_embedding_size],
                tf.int32, Character indices.
            text_len: tf.int32, [Amount of sentences]
            speaker_ids: [Amount of independent speakers], tf.int32, Speaker IDs.
            genre: [Amount of independent genres], tf.int32, Genre
            is_training: tf.bool
            gold_starts: tf.int32, [Amount of gold mentions]
            gold_ends: tf.int32, [Amount of gold mentions]
            cluster_ids: tf.int32, [Amount of independent clusters]

        Returns: word_emb, char_index, text_len, speaker_ids, genre, is_training, gold_starts, gold_ends, cluster_ids;
        The same set of tensors as in the input, but with a corrected shape.

        Additional Information:
        "None" in some form-size tensors, for example "word_emb", means that this axis measurement can vary
         from document to document.

        """
        max_training_sentences = self.max_training_sentences
        num_sentences = word_emb.shape[0]
        assert num_sentences > max_training_sentences

        sentence_offset = random.randint(0, num_sentences - max_training_sentences)

        word_offset = text_len[:sentence_offset].sum()

        # don't clear what exactly is happening here
        # why they cat the first part of tensor instead of second ???
        num_words = text_len[sentence_offset:sentence_offset + max_training_sentences].sum()
        word_emb = word_emb[sentence_offset:sentence_offset + max_training_sentences, :, :]
        char_index = char_index[sentence_offset:sentence_offset + max_training_sentences, :, :]
        text_len = text_len[sentence_offset:sentence_offset + max_training_sentences]

        speaker_ids = speaker_ids[word_offset: word_offset + num_words]

        assert len(gold_ends) == len(gold_starts)
        gold_starts_ = np.zeros((len(gold_starts)))
        gold_ends_ = np.zeros((len(gold_ends)))
        for i in range(len(gold_ends)):
            gold_ends_[i] = int(gold_ends[i])
            gold_starts_[i] = int(gold_starts[i])
        gold_starts = gold_starts_
        gold_ends = gold_ends_

        gold_spans = np.logical_and(gold_ends >= word_offset, gold_starts < word_offset + num_words)

        gold_starts = gold_starts[gold_spans] - word_offset
        gold_ends = gold_ends[gold_spans] - word_offset

        cluster_ids = cluster_ids[gold_spans]

        return word_emb, char_index, text_len, speaker_ids, genre, is_training, gold_starts, gold_ends, cluster_ids

    def get_mention_emb(self, text_emb, text_outputs, mention_starts, mention_ends):
        """
        Forms a tensor that contains of embeddings of specific mentions.
        Args:
            text_emb:  boolean mask, [num_sentences, max_sentence_length, emb]
            text_outputs: tf.float64, [num_sentences, max_sentence_length, emb]
            mention_starts: tf.int32, [Amount of mentions]
            mention_ends: tf.int32, [Amount of mentions]

        Returns: tf.float64, [num_mentions, emb]
        Mentions embeddings tensor.

        """
        mention_emb_list = []

        mention_start_emb = tf.gather(text_outputs, mention_starts)  # [num_mentions, emb]
        mention_emb_list.append(mention_start_emb)

        mention_end_emb = tf.gather(text_outputs, mention_ends)  # [num_mentions, emb]
        mention_emb_list.append(mention_end_emb)

        mention_width = 1 + mention_ends - mention_starts  # [num_mentions]
        if self.use_features:
            mention_width_index = mention_width - 1  # [num_mentions]
            mention_width_emb = tf.gather(tf.get_variable("mention_width_embeddings", [self.max_mention_width,
                                                                                       self.feature_size],
                                                          dtype=tf.float64),
                                          mention_width_index)  # [num_mentions, emb]
            mention_width_emb = tf.nn.dropout(mention_width_emb, self.dropout)
            mention_emb_list.append(mention_width_emb)

        if self.model_heads:
            mention_indices = tf.expand_dims(tf.range(self.max_mention_width), 0) + tf.expand_dims(
                mention_starts, 1)  # [num_mentions, max_mention_width]
            mention_indices = tf.minimum(custom_layers.shape(text_outputs, 0) - 1,
                                         mention_indices)  # [num_mentions, max_mention_width]
            mention_text_emb = tf.gather(text_emb, mention_indices)  # [num_mentions, max_mention_width, emb]

            self.head_scores = custom_layers.projection(text_outputs, 1)  # [num_words, 1]

            mention_head_scores = tf.gather(self.head_scores, mention_indices)  # [num_mentions, max_mention_width, 1]
            mention_mask = tf.expand_dims(tf.sequence_mask(mention_width, self.max_mention_width, dtype=tf.float64), 2)
            # [num_mentions, max_mention_width, 1]

            mention_attention = tf.nn.softmax(mention_head_scores + tf.log(mention_mask),
                                              dim=1)  # [num_mentions, max_mention_width, 1]
            mention_head_emb = tf.reduce_sum(mention_attention * mention_text_emb, 1)  # [num_mentions, emb]
            mention_emb_list.append(mention_head_emb)

        mention_emb = tf.concat(mention_emb_list, 1)  # [num_mentions, emb]
        return mention_emb

    def get_mention_scores(self, mention_emb):
        """
        Sends a mentions tensor to the input of a fully connected network, and outputs its output.
        It compute mentions scores.
        Args:
            mention_emb: tf.float64, [num_mentions, emb], a tensor that contains of embeddings of specific mentions

        Returns: [num_mentions, 1]
            Output of the fully-connected network, that compute the mentions scores.
        """
        with tf.variable_scope("mention_scores"):
            return custom_layers.ffnn(mention_emb, self.ffnn_depth, self.ffnn_size, 1, self.dropout)
            # [num_mentions, 1]

    @staticmethod
    def softmax_loss(antecedent_scores, antecedent_labels):
        """
        Computes the value of the loss function using antecedent_scores and antecedent_labels.
        Practically standard softmax function.
        Args:
            antecedent_scores: tf.float64, [num_mentions, max_ant + 1], output of fully-connected network that compute
                antecedent scores.
            antecedent_labels:  True labels for antecedent.

        Returns: [num_mentions]
            The value of loss function.
        """
        gold_scores = antecedent_scores + tf.log(tf.cast(antecedent_labels, tf.float64))  # [num_mentions, max_ant + 1]
        marginalized_gold_scores = tf.reduce_logsumexp(gold_scores, [1])  # [num_mentions]
        log_norm = tf.reduce_logsumexp(antecedent_scores, [1])  # [num_mentions]
        return log_norm - marginalized_gold_scores  # [num_mentions]

    def get_antecedent_scores(self, mention_emb, mention_scores, antecedents, antecedents_len, mention_speaker_ids,
                              genre_emb):
        """
        Forms a new tensor using special features, mentions embeddings, mentions scores, etc.
        and passes it through a fully-connected network that compute antecedent scores.
        Args:
            mention_emb: [num_mentions, emb], a tensor that contains of embeddings of specific mentions
            mention_scores: [num_mentions, 1], Output of the fully-connected network, that compute the mentions scores.
            antecedents: [] get from C++ function
            antecedents_len: [] get from C++ function
            mention_speaker_ids: [num_mentions, speaker_emb_size], tf.float64, Speaker IDs.
            genre_emb: [genre_emb_size], tf.float64, Genre

        Returns: tf.float64, [num_mentions, max_ant + 1], antecedent scores.

        """
        num_mentions = custom_layers.shape(mention_emb, 0)
        max_antecedents = custom_layers.shape(antecedents, 1)

        feature_emb_list = []

        if self.use_metadata:
            antecedent_speaker_ids = tf.gather(mention_speaker_ids, antecedents)  # [num_mentions, max_ant]
            same_speaker = tf.equal(tf.expand_dims(mention_speaker_ids, 1),
                                    antecedent_speaker_ids)  # [num_mentions, max_ant]
            speaker_pair_emb = tf.gather(tf.get_variable("same_speaker_emb", [2, self.feature_size],
                                                         dtype=tf.float64),
                                         tf.to_int32(same_speaker))  # [num_mentions, max_ant, emb]
            feature_emb_list.append(speaker_pair_emb)

            tiled_genre_emb = tf.tile(tf.expand_dims(tf.expand_dims(genre_emb, 0), 0),
                                      [num_mentions, max_antecedents, 1])  # [num_mentions, max_ant, emb]
            feature_emb_list.append(tiled_genre_emb)

        if self.use_features:
            target_indices = tf.range(num_mentions)  # [num_mentions]
            mention_distance = tf.expand_dims(target_indices, 1) - antecedents  # [num_mentions, max_ant]
            mention_distance_bins = self.distance_bins(mention_distance)  # [num_mentions, max_ant]
            mention_distance_bins.set_shape([None, None])
            mention_distance_emb = tf.gather(tf.get_variable("mention_distance_emb", [10, self.feature_size],
                                                             dtype=tf.float64),
                                             mention_distance_bins)  # [num_mentions, max_ant]
            feature_emb_list.append(mention_distance_emb)

        feature_emb = tf.concat(feature_emb_list, 2)  # [num_mentions, max_ant, emb]
        feature_emb = tf.nn.dropout(feature_emb, self.dropout)  # [num_mentions, max_ant, emb]

        antecedent_emb = tf.gather(mention_emb, antecedents)  # [num_mentions, max_ant, emb]
        target_emb_tiled = tf.tile(tf.expand_dims(mention_emb, 1),
                                   [1, max_antecedents, 1])  # [num_mentions, max_ant, emb]
        similarity_emb = antecedent_emb * target_emb_tiled  # [num_mentions, max_ant, emb]

        pair_emb = tf.concat([target_emb_tiled, antecedent_emb, similarity_emb, feature_emb], 2)
        # [num_mentions, max_ant, emb]

        with tf.variable_scope("iteration"):
            with tf.variable_scope("antecedent_scoring"):
                antecedent_scores = custom_layers.ffnn(pair_emb, self.ffnn_depth, self.ffnn_size, 1,
                                                       self.dropout)  # [num_mentions, max_ant, 1]

        antecedent_scores = tf.squeeze(antecedent_scores, 2)  # [num_mentions, max_ant]

        antecedent_mask = tf.log(
            tf.sequence_mask(antecedents_len, max_antecedents, dtype=tf.float64))  # [num_mentions, max_ant]
        antecedent_scores += antecedent_mask  # [num_mentions, max_ant]

        antecedent_scores += tf.expand_dims(mention_scores, 1) + tf.gather(mention_scores,
                                                                           antecedents)  # [num_mentions, max_ant]
        antecedent_scores = tf.concat([tf.zeros([custom_layers.shape(mention_scores, 0), 1], dtype=tf.float64),
                                       antecedent_scores],
                                      1)  # [num_mentions, max_ant + 1]
        return antecedent_scores  # [num_mentions, max_ant + 1]

    @staticmethod
    def flatten_emb_by_sentence(emb, text_len_mask):
        """
        Create boolean mask for emb tensor.
        Args:
            emb: Some embeddings tensor with rank 2 or 3
            text_len_mask: A mask tensor representing the first N positions of each row.

        Returns: emb tensor after mask applications.

        """
        num_sentences = tf.shape(emb)[0]
        max_sentence_length = tf.shape(emb)[1]

        emb_rank = len(emb.get_shape())
        if emb_rank == 2:
            flattened_emb = tf.reshape(emb, [num_sentences * max_sentence_length])
        elif emb_rank == 3:
            flattened_emb = tf.reshape(emb, [num_sentences * max_sentence_length, custom_layers.shape(emb, 2)])
        else:
            raise ValueError("Unsupported rank: {}".format(emb_rank))
        return tf.boolean_mask(flattened_emb, text_len_mask)

    def encode_sentences(self, text_emb, text_len, text_len_mask):
        """
        Passes the input tensor through bi_LSTM.
        Args:
            text_emb: [num_sentences, max_sentence_length, emb], text code in tensor
            text_len: tf.int32, [Amount of sentences]
            text_len_mask: boolean mask for text_emb

        Returns: [num_sentences, max_sentence_length, emb], output of bi-LSTM after boolean mask application

        """
        num_sentences = tf.shape(text_emb)[0]
        # max_sentence_length = tf.shape(text_emb)[1]

        # Transpose before and after for efficiency.
        inputs = tf.transpose(text_emb, [1, 0, 2])  # [max_sentence_length, num_sentences, emb]

        with tf.variable_scope("fw_cell"):
            cell_fw = custom_layers.CustomLSTMCell(self.lstm_size, num_sentences, self.dropout)
            preprocessed_inputs_fw = cell_fw.preprocess_input(inputs)
        with tf.variable_scope("bw_cell"):
            cell_bw = custom_layers.CustomLSTMCell(self.lstm_size, num_sentences, self.dropout)
            preprocessed_inputs_bw = cell_bw.preprocess_input(inputs)
            preprocessed_inputs_bw = tf.reverse_sequence(preprocessed_inputs_bw,
                                                         seq_lengths=text_len,
                                                         seq_dim=0,
                                                         batch_dim=1)
        state_fw = tf.contrib.rnn.LSTMStateTuple(tf.tile(cell_fw.initial_state.c, [num_sentences, 1]),
                                                 tf.tile(cell_fw.initial_state.h, [num_sentences, 1]))
        state_bw = tf.contrib.rnn.LSTMStateTuple(tf.tile(cell_bw.initial_state.c, [num_sentences, 1]),
                                                 tf.tile(cell_bw.initial_state.h, [num_sentences, 1]))
        with tf.variable_scope("lstm"):
            with tf.variable_scope("fw_lstm"):
                fw_outputs, fw_states = tf.nn.dynamic_rnn(cell=cell_fw,
                                                          inputs=preprocessed_inputs_fw,
                                                          sequence_length=text_len,
                                                          initial_state=state_fw,
                                                          time_major=True)
            with tf.variable_scope("bw_lstm"):
                bw_outputs, bw_states = tf.nn.dynamic_rnn(cell=cell_bw,
                                                          inputs=preprocessed_inputs_bw,
                                                          sequence_length=text_len,
                                                          initial_state=state_bw,
                                                          time_major=True)

        bw_outputs = tf.reverse_sequence(bw_outputs,
                                         seq_lengths=text_len,
                                         seq_dim=0,
                                         batch_dim=1)

        text_outputs = tf.concat([fw_outputs, bw_outputs], 2)
        text_outputs = tf.transpose(text_outputs, [1, 0, 2])  # [num_sentences, max_sentence_length, emb]
        return self.flatten_emb_by_sentence(text_outputs, text_len_mask)

    @staticmethod
    def get_predicted_antecedents(antecedents, antecedent_scores):
        """
        Forms a list of predicted antecedent labels
        Args:
            antecedents: [] get from C++ function
            antecedent_scores: [num_mentions, max_ant + 1] output of fully-connected network
                that compute antecedent_scores

        Returns: a list of predicted antecedent labels

        """
        predicted_antecedents = []
        for i, index in enumerate(np.argmax(antecedent_scores, axis=1) - 1):
            if index < 0:
                predicted_antecedents.append(-1)
            else:
                predicted_antecedents.append(antecedents[i, index])
        return predicted_antecedents

    def get_predictions_and_loss(self, word_emb, char_index, text_len, speaker_ids, genre, is_training, gold_starts,
                                 gold_ends, cluster_ids):
        """
        Connects all elements of the network to one complete graph, that compute mentions spans independently
        And passes through it the tensors that came to the input of placeholders.
        Args:
            word_emb: [Amount of sentences, Amount of words in sentence (max len), self.embedding_size],
                float64, Text embeddings.
            char_index: [Amount of words, Amount of chars in word (max len), char_embedding_size],
                tf.int32, Character indices.
            text_len: tf.int32, [Amount of sentences]
            speaker_ids: [Amount of independent speakers], tf.int32, Speaker IDs.
            genre: [Amount of independent genres], tf.int32, Genre
            is_training: tf.bool
            gold_starts: tf.int32, [Amount of gold mentions]
            gold_ends: tf.int32, [Amount of gold mentions]
            cluster_ids: tf.int32, [Amount of independent clusters]

        Returns:[candidate_starts, candidate_ends, candidate_mention_scores, mention_starts, mention_ends, antecedents,
                antecedent_scores], loss
        List of predictions and scores, and Loss function value
        """
        self.dropout = 1 - (tf.cast(is_training, tf.float64) * self.dropout_rate)
        self.lexical_dropout = 1 - (tf.cast(is_training, tf.float64) * self.lexical_dropout_rate)

        num_sentences = tf.shape(word_emb)[0]
        max_sentence_length = tf.shape(word_emb)[1]

        text_emb_list = [word_emb]

        if self.char_embedding_size > 0:
            char_emb = tf.gather(
                tf.get_variable("char_embeddings", [len(self.char_dict), self.char_embedding_size]),
                char_index)  # [num_sentences, max_sentence_length, max_word_length, emb]
            flattened_char_emb = tf.reshape(char_emb, [num_sentences * max_sentence_length,
                                                       custom_layers.shape(char_emb, 2),
                                                       custom_layers.shape(char_emb, 3)])
            # [num_sentences * max_sentence_length, max_word_length, emb]

            flattened_aggregated_char_emb = custom_layers.cnn(flattened_char_emb, self.filter_widths,
                                                              self.filter_size)
            # [num_sentences * max_sentence_length, emb]

            aggregated_char_emb = tf.reshape(flattened_aggregated_char_emb,
                                             [num_sentences,
                                              max_sentence_length,
                                              custom_layers.shape(flattened_aggregated_char_emb, 1)])
            # [num_sentences, max_sentence_length, emb]

            text_emb_list.append(aggregated_char_emb)

        text_emb = tf.concat(text_emb_list, 2)
        text_emb = tf.nn.dropout(text_emb, self.lexical_dropout)

        text_len_mask = tf.sequence_mask(text_len, maxlen=max_sentence_length)
        text_len_mask = tf.reshape(text_len_mask, [num_sentences * max_sentence_length])

        text_outputs = self.encode_sentences(text_emb, text_len, text_len_mask)
        text_outputs = tf.nn.dropout(text_outputs, self.dropout)

        genre_emb = tf.gather(tf.get_variable("genre_embeddings",
                                              [len(self.genres), self.feature_size],
                                              dtype=tf.float64),
                              genre)  # [emb]
        # -------------------------------------------------------------------------------------------------------------
        flattened_text_emb = self.flatten_emb_by_sentence(text_emb, text_len_mask)  # [num_words]

        if self.train_on_gold:
            candidate_mention_emb = self.get_mention_emb(flattened_text_emb, text_outputs, gold_starts,
                                                         gold_ends)  # [num_candidates, emb]
            gold_len = tf.shape(gold_ends)
            candidate_mention_scores = tf.ones(gold_len, dtype=tf.float64)

            mention_starts = gold_starts
            mention_ends = gold_ends
            mention_emb = candidate_mention_emb
            mention_scores = candidate_mention_scores
        else:
            sentence_indices = tf.tile(tf.expand_dims(tf.range(num_sentences), 1),
                                       [1, max_sentence_length])  # [num_sentences, max_sentence_length]
            flattened_sentence_indices = self.flatten_emb_by_sentence(sentence_indices, text_len_mask)  # [num_words]

            candidate_starts, candidate_ends = self.spans(
                sentence_indices=flattened_sentence_indices,
                max_width=self.max_mention_width)
            candidate_starts.set_shape([None])
            candidate_ends.set_shape([None])

            candidate_mention_emb = self.get_mention_emb(flattened_text_emb, text_outputs, candidate_starts,
                                                         candidate_ends)  # [num_candidates, emb]

            candidate_mention_scores = self.get_mention_scores(candidate_mention_emb)  # [num_mentions, 1]
            candidate_mention_scores = tf.squeeze(candidate_mention_scores, 1)  # [num_mentions]

            k = tf.to_int32(tf.floor(tf.to_float(tf.shape(text_outputs)[0]) * self.mention_ratio))
            predicted_mention_indices = self.extract_mentions(candidate_mention_scores, candidate_starts,
                                                              candidate_ends, k)  # ([k], [k])
            predicted_mention_indices.set_shape([None])

            mention_starts = tf.gather(candidate_starts, predicted_mention_indices)  # [num_mentions]
            mention_ends = tf.gather(candidate_ends, predicted_mention_indices)  # [num_mentions]
            mention_emb = tf.gather(candidate_mention_emb, predicted_mention_indices)  # [num_mentions, emb]
            mention_scores = tf.gather(candidate_mention_scores, predicted_mention_indices)  # [num_mentions]

        # mention_start_emb = tf.gather(text_outputs, mention_starts)  # [num_mentions, emb]
        # mention_end_emb = tf.gather(text_outputs, mention_ends)  # [num_mentions, emb]
        mention_speaker_ids = tf.gather(speaker_ids, mention_starts)  # [num_mentions]

        max_antecedents = self.max_antecedents
        antecedents, antecedent_labels, antecedents_len = self.get_antecedents(mention_starts, mention_ends,
                                                                               gold_starts, gold_ends, cluster_ids,
                                                                               max_antecedents)
        # ([num_mentions, max_ant], [num_mentions, max_ant + 1], [num_mentions]
        # -------------------------------------------------------------------------------------------------------------

        antecedents.set_shape([None, None])
        antecedent_labels.set_shape([None, None])
        antecedents_len.set_shape([None])

        antecedent_scores = self.get_antecedent_scores(mention_emb, mention_scores, antecedents, antecedents_len,
                                                       mention_speaker_ids, genre_emb)  # [num_mentions, max_ant + 1]

        loss = self.softmax_loss(antecedent_scores, antecedent_labels)  # [num_mentions]
        loss = tf.reduce_sum(loss)  # []

        # [candidate_starts, candidate_ends, candidate_mention_scores, mention_starts, mention_ends,
        #                     antecedents, antecedent_scores], loss

        return [candidate_mention_scores, mention_starts, mention_ends, antecedents, antecedent_scores], loss

    @staticmethod
    def get_predicted_clusters(mention_starts, mention_ends, predicted_antecedents):
        """
        Creates a list of clusters, as in dict from observation, and dict mentions with a list of clusters
        to which they belong. They are necessary for inference mode and marking a new conll documents without
        last column.
        Args:
            mention_starts: tf.float64, [Amount of mentions]
            mention_ends: tf.float64, [Amount of mentions]
            predicted_antecedents: [len antecedent scores]

        Returns:
            predicted_clusters = [[(),(),()],[(),()]] list like, with mention id
            mention_to_predicted = {mentions id: [(),(),()], ...}
        """
        mention_to_predicted = {}
        predicted_clusters = []
        for i, predicted_index in enumerate(predicted_antecedents):
            if predicted_index < 0:
                continue
            assert i > predicted_index
            predicted_antecedent = (int(mention_starts[predicted_index]), int(mention_ends[predicted_index]))
            if predicted_antecedent in mention_to_predicted:
                predicted_cluster = mention_to_predicted[predicted_antecedent]
            else:
                predicted_cluster = len(predicted_clusters)
                predicted_clusters.append([predicted_antecedent])
                mention_to_predicted[predicted_antecedent] = predicted_cluster

            mention = (int(mention_starts[i]), int(mention_ends[i]))
            predicted_clusters[predicted_cluster].append(mention)
            mention_to_predicted[mention] = predicted_cluster

        predicted_clusters = [tuple(pc) for pc in predicted_clusters]
        mention_to_predicted = {m: predicted_clusters[i] for m, i in mention_to_predicted.items()}

        return predicted_clusters, mention_to_predicted

    def train_on_batch(self, *args):
        """
        Run train operation on one batch/document
        Args:
            args: (sentences, speakers, doc_key, clusters) list of text documents, list of authors, list of files names,
             list of true clusters

        Returns: Loss functions value and tf.global_step

        """
        if self.train_on_gold:
            sentences, speakers, doc_key, mentions_st, clusters = args
        else:
            sentences, speakers, doc_key, clusters = args
        batch = {"sentences": sentences, "speakers": speakers, "doc_key": doc_key, "clusters": clusters}
        self.start_enqueue_thread(batch, True)
        self.tf_loss, tf_global_step, _ = self.sess.run([self.loss, self.global_step, self.train_op])
        return self.tf_loss

    def __call__(self, *args):
        if self.train_on_gold:
            sentences, speakers, doc_key, clusters = args
            batch = {"sentences": sentences, "speakers": speakers, "doc_key": doc_key, "clusters": clusters}
        else:
            sentences, speakers, doc_key = args
            batch = {"sentences": sentences, "speakers": speakers, "doc_key": doc_key, "clusters": []}

        self.start_enqueue_thread(batch, False)

        _, mention_starts, mention_ends, antecedents, antecedent_scores = self.sess.run(self.predictions)

        predicted_antecedents = self.get_predicted_antecedents(antecedents, antecedent_scores)

        predicted_clusters, mention_to_predicted = self.get_predicted_clusters(mention_starts, mention_ends,
                                                                               predicted_antecedents)

        return [predicted_clusters], [mention_to_predicted]

    def destroy(self):
        """Reset the model"""
        self.sess.close()
