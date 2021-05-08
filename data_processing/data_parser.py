from scipy.ndimage.interpolation import zoom
from os.path import isfile, join
from os import listdir
from tqdm import tqdm

import tensorflow as tf
import numpy as np
import cv2
AUTOTUNE = tf.data.experimental.AUTOTUNE
BATCH_SIZE = 24
N_CLASSES = 9
BUFFER_SIZE = 50


class DataWriter():
    def __init__(self, src_path, dest_path, batch_size=25, height=512, width=512):
        self.src_path = src_path
        self.dest_path = dest_path
        self.filenames = [f for f in listdir(
            src_path) if isfile(join(src_path, f))]
        np.random.shuffle(self.filenames)
        self.batch_size = batch_size
        self.n_samples = len(self.filenames)
        self.height = height
        self.width = width

    @staticmethod
    def _bytes_feature(value):
        """Returns a bytes_list from a string / byte."""
        if isinstance(value, type(tf.constant(0))):  # if value ist tensor
            value = value.numpy()  # get value of tensor
        return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))

    @staticmethod
    def serialize_array(array):
        array = tf.io.serialize_tensor(array)
        return array

    def parse_single_image(self, image, label):
        # define the dictionary -- the structure -- of our single example
        data = {
            'image': self._bytes_feature(self.serialize_array(image)),
            'label': self._bytes_feature(self.serialize_array(label))
        }
        # create an Example, wrapping the single features
        out = tf.train.Example(features=tf.train.Features(feature=data))
        return out

    def write_image_to_tfr(self, image, label, filename):
        filename = filename+"tfrecords"
        # create a writer that'll store our data to disk
        writer = tf.io.TFRecordWriter(filename)

        image_rgb = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        out = self.parse_single_image(image=image_rgb, label=label)
        writer.write(out.SerializeToString())

        writer.close()
        print(f"Wrote {filename} elements to TFRecord")

    def write_tfrecords(self):
        for file in tqdm(self.filenames):
            data = np.load(self.src_path + file)
            image, label = data['image'], data['label']
            filename = self.dest_path + file[:-3]
            self.write_image_to_tfr(image, label, filename)

    def process_data(self, data):
        image = cv2.cvtColor(data['image'], cv2.COLOR_GRAY2RGB)
        w, h, c = image.shape
        if w != self.width or h != self.height:
            image = zoom(
                image, (self.width / w, self.height / h, 1), order=3)
            label = zoom(data['label'], (self.width /
                                         w, self.height / h), order=0)
        return image, label

    def write_batch_tfrecords(self):
        n_batches = self.n_samples // self.batch_size
        for i in tqdm(range(n_batches+1)):
            filename = self.dest_path + f'record_{i}.tfrecords'
            writer = tf.io.TFRecordWriter(filename)
            start, end = self.batch_size*i, self.batch_size * \
                (i+1) if self.batch_size * \
                (i+1) < self.n_samples else self.n_samples
            for file in self.filenames[start: end]:
                data = np.load(self.src_path + file)
                image, label = self.process_data(data)
                out = self.parse_single_image(image=image, label=label)
                writer.write(out.SerializeToString())
            writer.close()
            print(f"Wrote batch {i} to TFRecord")


class DataReader():

    def __init__(self, src_path="", height=512, width=512, depth=3):
        self.src_path = src_path
        self.filenames = [self.src_path + f for f in listdir(
            src_path) if isfile(join(src_path, f))]
        self.height = height
        self.width = width
        self.depth = depth

    def parse_tfr_element(self, element):
        data = {
            'label': tf.io.FixedLenFeature([], tf.string),
            'image': tf.io.FixedLenFeature([], tf.string),
        }

        content = tf.io.parse_single_example(element, data)
        raw_label = content['label']
        raw_image = content['image']

        image = tf.io.parse_tensor(raw_image, out_type=tf.float32)
        image = tf.reshape(image, shape=[self.height, self.width, self.depth])

        label = tf.io.parse_tensor(raw_label, out_type=tf.float32)
        label = tf.reshape(label, shape=[self.height, self.width])
        # label = tf.cast(label, tf.int32)
        # label = tf.one_hot(label, depth=N_CLASSES)
        return (image, label)

    def get_dataset_small(self, filenames=None):
        # create the dataset
        filenames = self.filenames if filenames is None else filenames
        dataset = tf.data.TFRecordDataset(filenames)
        # pass every single feature through our mapping function
        dataset = dataset.map(
            self.parse_tfr_element
        )
        return dataset

    def load_dataset(self, filenames=None):
        filenames = self.filenames if filenames is None else filenames
        ignore_order = tf.data.Options()
        ignore_order.experimental_deterministic = False  # disable order, increase speed
        dataset = tf.data.TFRecordDataset(
            filenames, num_parallel_reads=AUTOTUNE)  # automatically interleaves reads from multiple files
        # uses data as soon as it streams in, rather than in its original order
        dataset = dataset.with_options(ignore_order)
        dataset = dataset.map(
            self.parse_tfr_element, num_parallel_calls=AUTOTUNE
        )
        # returns a dataset of (image, label) pairs if labeled=True or just images if labeled=False
        return dataset

    def get_dataset(self, filenames=None):
        filenames = self.filenames if filenames is None else filenames
        dataset = self.load_dataset(filenames)
        dataset = dataset.shuffle(BUFFER_SIZE)
        dataset = dataset.prefetch(buffer_size=AUTOTUNE)
        dataset = dataset.batch(BATCH_SIZE)
        return dataset

    def load_dataset_tpu(self, filenames):
      # Read from TFRecords. For optimal performance, we interleave reads from multiple files.
        records = tf.data.TFRecordDataset(
            filenames, num_parallel_reads=AUTOTUNE)
        return records.map(self.parse_tfr_element, num_parallel_calls=AUTOTUNE)

    def get_training_dataset(self, train_fns, batch_size):
        dataset = self.load_dataset_tpu(train_fns)

        # Create some additional training images by randomly flipping and
        # increasing/decreasing the saturation of images in the training set.
        # def data_augment(image, one_hot_class):
        #     modified = tf.image.random_flip_left_right(image)
        #     modified = tf.image.random_saturation(modified, 0, 2)
        #     return modified, one_hot_class
        # augmented = dataset.map(data_augment, num_parallel_calls=AUTOTUNE)
        # return augmented.repeat().shuffle(BUFFER_SIZE).batch(batch_size).prefetch(AUTOTUNE)

        # Prefetch the next batch while training (autotune prefetch buffer size).
        return dataset.shuffle(BUFFER_SIZE).batch(batch_size, drop_remainder=True).prefetch(AUTOTUNE)

    def get_dataset_tpu_training(self, tpu_strategy):

        batch_size = 16 * tpu_strategy.num_replicas_in_sync
        # batch_size=BATCH_SIZE
        gcs_pattern = 'gs://aga_bucket/synapse-tfrecords-batch25/*.tfrecords'
        validation_split = 0.1
        filenames = tf.io.gfile.glob(gcs_pattern)
        split = len(filenames) - int(len(filenames) * validation_split)
        train_fns = filenames[:split]
        validation_fns = filenames[split:]

        training_dataset = self.get_training_dataset(train_fns, batch_size)
        validation_dataset = self.load_dataset(
            validation_fns).batch(batch_size, drop_remainder=True).prefetch(AUTOTUNE)

        return training_dataset, validation_dataset
