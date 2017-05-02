import tensorflow as tf
import numpy as np
from math import ceil
import sys
import os
from attention_cell import AttentionBasedGRUCell


class DMN(object):
    """docstring for DMN"""
    def __init__(self, config, word_vectors=[]):
        self.dim_proj = config['dim_proj']
        self.layers = config['layers']
        self.batch_size = tf.placeholder(tf.int32, name="batch_size")
        self.n_words = config['n_words']
        self.learning_rate = config['learning_rate']
        self.num_classes = config['classes_num']
        self.word_vectors = word_vectors
        self.global_step = tf.Variable(0, name="global_step", trainable=False)
        self.config = config
        self.input_keep_prob = tf.placeholder(
            tf.float32, name="keep_prob_inp")
        self.output_keep_prob = tf.placeholder(
            tf.float32, name="keep_prob_out")
        self.sentence_len = config['sentence_len']
        self.max_gradient_norm = config["clip_threshold"]
        self.x = tf.placeholder(tf.int32, [None, self.sentence_len], name="x")
        self.y = tf.placeholder(tf.float32, [None, self.num_classes], name="y")
        self.question = tf.placeholder(
            tf.int32, [None, self.sentence_len], name="question_")

        self.dropout_prob = tf.placeholder(tf.float32, name="dropout_prob")

        # values vital when using minibatches on dev set
        # really ugly workaround
        self.metrics_weight = tf.placeholder(tf.float32, name="metrics_weight")
        self.fixed_acc_value = tf.placeholder(tf.float32, name="f_acc_value")
        self.fixed_loss_value = tf.placeholder(tf.float32, name="f_loss_value")
        self.all_attentions = []

        self.build_input()
        self.encoder()
        self.question_module()
        self.episodic_module()
        self.answer_module()

        self.train()
        self.summarize()

    def build_input(self):
        """ declare graph """
        with tf.name_scope("word_embeddings"):
            """
            initialize embeddings currently able to use only one type of
            embeddings
            """
            index_ = 0
            self.w_embeddings = tf.get_variable(
                "W0_" + str(index_),
                shape=[self.n_words, self.dim_proj],
                trainable=self.config['train_embeddings'][index_],
                initializer=tf.constant_initializer(
                    np.array(self.word_vectors[index_]))
            )
            embedded_tokens = tf.nn.embedding_lookup(
                self.w_embeddings, self.x)
            self.rnn_input = embedded_tokens

            self.seq_lengths = self.get_seq_lenghts("facts", self.x)

    def encoder(self):
        with tf.name_scope("rnn_cell"):
            if self.config['GRU']:  # use GRU cell
                self.rnn_cell = tf.contrib.rnn.DropoutWrapper(
                    tf.contrib.rnn.GRUCell(num_units=self.dim_proj),
                    input_keep_prob=self.input_keep_prob,
                    output_keep_prob=self.output_keep_prob
                )
            else:
                raise ValueError("Currently only support GRU cell, change config")
        # create sequential rnn from single cells
        rnn_cell_seq = tf.contrib.rnn.MultiRNNCell(
            [self.rnn_cell] * self.layers, state_is_tuple=True)
        initial_state = rnn_cell_seq.zero_state(
            self.batch_size, tf.float32)

        if self.config['bidirectional']:
            self.dimensionality_mult = 2
            output, states = tf.nn.bidirectional_dynamic_rnn(
                inputs=self.rnn_input,
                cell_fw=rnn_cell_seq,
                cell_bw=rnn_cell_seq,
                initial_state_fw=initial_state,
                initial_state_bw=initial_state,
                sequence_length=self.seq_lengths
            )
            self.output = output

        else:
            self.dimensionality_mult = 1
            output, state = tf.nn.dynamic_rnn(
                rnn_cell_seq, self.rnn_input,
                initial_state=initial_state,
                sequence_length=self.seq_lengths
            )
            # self.out_state = state
            self.output = output

    def question_module(self):
        """ declare question module """
        with tf.name_scope("question"):
            """ transform question to sequence of vectors
            """
            self.input_question = tf.nn.embedding_lookup(
                self.w_embeddings, self.question)
            self.seq_lengths_q = self.get_seq_lenghts(
                "questions", self.question)

            # create sequential rnn from single cells
            rnn_cell = tf.contrib.rnn.DropoutWrapper(
                tf.contrib.rnn.GRUCell(num_units=self.dim_proj),
                input_keep_prob=1.0,
                output_keep_prob=1.0
            )
            rnn_cell_seq = tf.contrib.rnn.MultiRNNCell(
                [rnn_cell] * 1, state_is_tuple=True)
            initial_state = rnn_cell_seq.zero_state(
                self.batch_size, tf.float32)

            output, state = tf.nn.dynamic_rnn(
                rnn_cell_seq, self.input_question,
                initial_state=initial_state,
                sequence_length=self.seq_lengths_q
            )
            self.output_q = state[-1]

    def episodic_module(self):
        """takes the final state of question module
        and the sequence of outputs of our encoder
        and produces 'memories' using an rnn.
        uses attention over the encoders' states
        """
        with tf.name_scope("episodic_module"):
            memory = self.output_q
            for i in range(self.config["episodes_num"]):
                # calc new sentence representations
                c_t = self.attention(self.output, self.output_q, memory)
                # update memory
                memory = self.memory_update(memory, c_t, self.output_q)
            self.last_memory = memory

    def answer_module(self):
        """take the final state/episode of episodic module  and
        producess an answer (here a simplified version without an RNN
        to build the answer, using just a fc layer)"""
        with tf.name_scope("answer_module"):
            # initial state is the last memory
            # initial_state = self.last_memory
            shape = [int(self.last_memory.shape[1]), self.num_classes]
            W = tf.Variable(
                tf.truncated_normal(shape, stddev=0.01), name="W_answer",
            )
            b = tf.Variable(tf.constant(
                0.1, shape=[self.num_classes]),
                trainable=True, name="b_answer"
            )
            self.scores = tf.nn.xw_plus_b(self.last_memory, W, b)

    def attention(self, facts, question, prev_memory):
        ''' calculate the attention distribution for a batch of sentences
        using a combination of our facts, the question and the previous episode
        '''

        g_tensor = tf.Variable(tf.truncated_normal(
            [1, self.dim_proj]), name="g_scores")
        i = tf.constant(0)
        with tf.name_scope("attention_mechanism", reuse=True):
            with tf.name_scope("attention_gates"):

                def condition(i, g_tensor):
                    return tf.less(i, self.batch_size)

                def body(i, g_tensor):
                    up_to = self.seq_lengths[i]
                    # get facts of a single instance from batch
                    fact = tf.slice(
                        self.facts, [i, 0, 0], [1, up_to, -1])
                    fact = tf.reshape(fact, [up_to, -1])
                    # calculate z_i
                    z_i = [tf.multiply(fact, question),
                           tf.multiply(fact, prev_memory),
                           tf.abs(fact - question),
                           tf.abs(fact - prev_memory)]
                    z_i_c = tf.concat(z_i, 1)
                    print ("z_i_c = {}".format(z_i_c.shape))

                    # calculate attention values
                    w_1_dim = 256
                    hidden_z_shape = [int(z_i_c.shape[1]), w_1_dim]
                    W_1 = tf.get__variable(
                        "att_weight_1", shape=hidden_z_shape,
                        initializer=tf.contrib.layers.xavier_initializer()
                    )
                    b_1 = tf.Variable(tf.constant(
                        0.1, shape=[w_1_dim]), name="att_bias_1")
                    inter = tf.nn.tanh(
                        tf.nn.xw_plus_b(z_i_c, W_1, b_1)
                    )

                    W_2 = tf.get_variable(
                        "att_weight_2", shape=[w_1_dim, 1],
                        initializer=tf.contrib.layers.xavier_initializer()
                    )
                    b_2 = tf.Variable(tf.constant(
                        0.1, shape=[1]), name="att_bias_2")

                    # slice b2 and w_2 to much instance i seq_length (WRONG)
                    # unorm_att = tf.nn.xw_plus_b(
                    #     inter, tf.slice(W_2, [0, 0], [-1, up_to]),
                    #     tf.slice(b_2, [0], [up_to]))
                    unorm_att = tf.nn.xw_plus_b(inter, W_2, b_2)
                    # softmax values g_i
                    g_i = tf.nn.softmax(unorm_att)
                    # zero pad attentions to fit in tensor
                    paddings = [[0, 0],
                                [0, self.sentence_len - self.seq_lengths[i]]]
                    padded_g_i = tf.pad(g_i, paddings, "padded_g_i")

                    g_tensor = tf.cond(
                        tf.equal(i, 0), lambda: padded_g_i, lambda: tf.concat(
                            [g_tensor, padded_g_i], axis=0)
                    )

                    i = tf.add(i, 1)
                    return [i, g_tensor]

                _, g_tensor = tf.while_loop(
                    condition, body, [i, g_tensor],
                    shape_invariants=[i.get_shape(),
                                      tf.TensorShape([None, None])]
                )
                self.all_attentions.append(g_tensor)
            with tf.name_scope("attention_GRU"):
                """ takes a zero padded attention tensor, and calculates
                using a gru the c_t vectors (i.e sentence representations)
                """
                # c_t = []
                # inputs to gru, take last attention distribution
                inputs = tf.concat([facts, self.all_attentions[-1]])
                print ("inputs {} shape {}".format(inputs, inputs.shape))
                attention_cell = tf.contrib.rnn.DropoutWrapper(
                    AttentionBasedGRUCell(num_units=self.dim_proj),
                    input_keep_prob=self.input_keep_prob,
                    output_keep_prob=self.output_keep_prob
                )
                rnn_cell_seq = tf.contrib.rnn.MultiRNNCell(
                    [attention_cell] * 1, state_is_tuple=True)
                initial_state = rnn_cell_seq.zero_state(
                    self.batch_size, tf.float32)
                _, c_t = tf.nn.dynamic_rnn(
                    inputs=inputs, cell=rnn_cell_seq,
                    sequence_length=self.seq_lengths,
                    initial_state=initial_state)

                return c_t

    def memory_update(self, prev_memory, c_t, question):
        with tf.name_scope("memory_update", reuse=False):
            # memory update based on
            # Sukhbaatar et al. (2015) and Peng et al. (2015)
            # m^t = ReLU(Wt[m^{t−1}; c^t; q]+b)
            # using untied weights
            input_ = [prev_memory, c_t, question]
            c_input = tf.concat(input_, 1)
            W = tf.get_variable(
                "w_mem_update", shape=[int(c_input.shape[1]), self.dim_proj],
                initializer=tf.contrib.layers.xavier_initializer())
            b = tf.Variable(tf.constant(0.1, shape=[self.dim_proj]),
                            name="b_mem_update")
            new_memory = tf.nn.relu(
                tf.nn.xw_plus_b(c_input, W, b))

            return new_memory

            # way less than 'half' finished gru memory update
            # cell = tf.contrib.rnn.DropoutWrapper(
            #     tf.contrib.rnn.GRUCell(num_units=self.dim_proj),
            #     input_keep_prob=self.input_keep_prob,
            #     output_keep_prob=self.output_keep_prob
            # )
            # mem_up_input = []
            # _, new_episode = tf.nn.dynamic_rnn(
            #     cell=cell,
            #     inputs=mem_up_input,
            #     initial_state=prev_memory,
            #     sequence_length=self.seq_length
            # )

    def train(self):
        """ calculate accuracies, train and predict """
        with tf.name_scope("predict"):
            self.predictions = tf.argmax(self.scores, 1)
            self.true_predictions = tf.argmax(self.y, 1)
            self.probs = tf.nn.softmax(self.scores)
        with tf.name_scope("loss"):
            self.losses = tf.nn.softmax_cross_entropy_with_logits(
                logits=self.scores, labels=self.y, name="losses")
            # self.total_loss = tf.reduce_sum(self.losses)
            self.mean_loss = tf.reduce_mean(self.losses)
        with tf.name_scope("accuracy"):
            self.correct_predictions = tf.equal(
                self.predictions, tf.argmax(self.y, 1))
            self.accuracy = tf.reduce_mean(
                tf.cast(self.correct_predictions, "float"), name="accuracy")

        params = tf.trainable_variables()

        with tf.name_scope("train"):
            optimizer = tf.train.AdamOptimizer(self.learning_rate)
        gradients = tf.gradients(self.mean_loss, params)
        clipped_gradients, norm = tf.clip_by_global_norm(
            gradients, self.max_gradient_norm)

        # with tf.name_scope("grad_norms"):
        #     grad_summ = tf.scalar_summary("grad_norms", norm)

        self.update = optimizer.apply_gradients(
            zip(clipped_gradients, params), global_step=self.global_step)

    def summarize(self):
        self.mean_loss = self.metrics_weight * self.mean_loss + \
            self.fixed_loss_value
        self.accuracy = self.metrics_weight * self.accuracy + \
            self.fixed_acc_value
        loss_summary = tf.summary.scalar("loss", self.mean_loss)
        acc_summary = tf.summary.scalar("accuracy", self.accuracy)
        # Summaries
        self.summary_op = tf.summary.merge([loss_summary, acc_summary])

    def get_seq_lenghts(self, type_, input_):
        '''
        calculate actual length of each sentence -- known bug
        if a sentence ends with unknown tokens they are not considered
        in size, if they are at start or in between they are
        considered though
        '''
        with tf.name_scope("calc_sequences_length_" + type_):
            # this doesn't work due to zero padding and <UNK> being both zero
            # self.seq_lengths = tf.reduce_sum(tf.sign(self.x), 1)
            mask = tf.sign(input_)
            range_ = tf.range(
                start=1, limit=self.sentence_len + 1, dtype=tf.int32)
            mask = tf.multiply(mask, range_, name="mask")
            return tf.reduce_max(mask, axis=1)
