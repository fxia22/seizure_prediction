from collections import namedtuple
import os.path
import numpy as np
import scipy.io
import common.time as time
from sklearn import cross_validation, preprocessing
from sklearn.metrics import roc_curve, auc
import random
from sklearn.isotonic import IsotonicRegression as IR

drate = 100
TaskCore = namedtuple('TaskCore', ['cached_data_loader', 'data_dir', 'target', 'pipeline', 'classifier_name',
                                   'classifier', 'normalize', 'gen_ictal', 'cv_ratio'])

class Task(object):
    """
    A Task computes some work and outputs a dictionary which will be cached on disk.
    If the work has been computed before and is present in the cache, the data will
    simply be loaded from disk and will not be pre-computed.
    """
    def __init__(self, task_core):
        self.task_core = task_core

    def filename(self):
        raise NotImplementedError("Implement this")

    def run(self):
        print 'filename:',self.filename()
        return self.task_core.cached_data_loader.load(self.filename(), self.load_data)


class LoadIctalDataTask(Task):
    """
    Load the ictal mat files 1 by 1, transform each 1-second segment through the pipeline
    and return data in the format {'X': X, 'Y': y, 'latencies': latencies}
    """
    def filename(self):
        return 'data_preictal_%s_%s' % (self.task_core.target, self.task_core.pipeline.get_name())

    def load_data(self):
        return parse_input_data(self.task_core.data_dir, self.task_core.target, 'preictal', self.task_core.pipeline,
                           self.task_core.gen_ictal)


class LoadInterictalDataTask(Task):
    """
    Load the interictal mat files 1 by 1, transform each 1-second segment through the pipeline
    and return data in the format {'X': X, 'Y': y}
    """
    def filename(self):
        return 'data_interictal_%s_%s' % (self.task_core.target, self.task_core.pipeline.get_name())

    def load_data(self):
        return parse_input_data(self.task_core.data_dir, self.task_core.target, 'interictal', self.task_core.pipeline)


class LoadTestDataTask(Task):
    """
    Load the test mat files 1 by 1, transform each 1-second segment through the pipeline
    and return data in the format {'X': X}
    """
    def filename(self):
        return 'data_test_%s_%s' % (self.task_core.target, self.task_core.pipeline.get_name())

    def load_data(self):
        return parse_input_data(self.task_core.data_dir, self.task_core.target, 'test', self.task_core.pipeline)


class TrainingDataTask(Task):
    """
    Creating a training set and cross-validation set from the transformed ictal and interictal data.
    """
    def filename(self):
        return None  # not cached, should be fast enough to not need caching

    def load_data(self):
        ictal_data = LoadIctalDataTask(self.task_core).run()
        interictal_data = LoadInterictalDataTask(self.task_core).run()
        return prepare_training_data(ictal_data, interictal_data, self.task_core.cv_ratio)


class CrossValidationScoreTask(Task):
    """
    Run a classifier over a training set, and give a cross-validation score.
    """
    def filename(self):
        return 'score_%s_%s_%s' % (self.task_core.target, self.task_core.pipeline.get_name(), self.task_core.classifier_name)

    def load_data(self):
        data = TrainingDataTask(self.task_core).run()
        classifier_data = train_classifier(self.task_core.classifier, data, normalize=self.task_core.normalize)
        del classifier_data['classifier'] # save disk space
        return classifier_data


class CrossValidationScoreFullTask(Task):
    """
    Run a classifier over a training set, and give a cross-validation score.
    """
    def filename(self):
        return 'score_%s_%s_%s' % (self.task_core.target, self.task_core.pipeline.get_name(), self.task_core.classifier_name)

    def load_data(self):
        data = TrainingDataTask(self.task_core).run()
        classifier_data = train_classifier(self.task_core.classifier, data, normalize=self.task_core.normalize, return_data=True)
        return classifier_data


class TrainClassifierTask(Task):
    """
    Run a classifier over the complete data set (training data + cross-validation data combined)
    and save the trained models.
    """
    def filename(self):
        return 'classifier_%s_%s_%s' % (self.task_core.target, self.task_core.pipeline.get_name(), self.task_core.classifier_name)

    def load_data(self):
        data = TrainingDataTask(self.task_core).run()
        return train_classifier(self.task_core.classifier, data, use_all_data=True, normalize=self.task_core.normalize)


class TrainClassifierwithCalibTask(Task):
    """
    Run a classifier over the complete data set (training data + cross-validation data combined)
    and save the trained models.
    """
    def filename(self):
        return 'classifier_%s_%s_%s' % (self.task_core.target, self.task_core.pipeline.get_name(), self.task_core.classifier_name)

    def load_data(self):
        data = TrainingDataTask(self.task_core).run()
        return train_classifier_with_calib(self.task_core.classifier, data, use_all_data=False, normalize=self.task_core.normalize)



class MakePredictionsTask(Task):
    """
    Make predictions on the test data.
    """
    def filename(self):
        return 'predictions_%s_%s_%s' % (self.task_core.target, self.task_core.pipeline.get_name(), self.task_core.classifier_name)

    def load_data(self):
        data = TrainingDataTask(self.task_core).run()
        y_classes = data.y_classes
        del data

        classifier_data = TrainClassifierTask(self.task_core).run()
        test_data = LoadTestDataTask(self.task_core).run()
        X_test = flatten(test_data.X)

        return make_predictions(self.task_core.target, X_test, y_classes, classifier_data)


class MakePredictionswithCalibTask(Task):
    """
    Make predictions on the test data.
    """
    def filename(self):
        return 'predictions_%s_%s_%s' % (self.task_core.target, self.task_core.pipeline.get_name(), self.task_core.classifier_name)

    def load_data(self):
        data = TrainingDataTask(self.task_core).run()
        y_classes = data.y_classes
        del data

        classifier_data = TrainClassifierwithCalibTask(self.task_core).run()
        test_data = LoadTestDataTask(self.task_core).run()
        X_test = flatten(test_data.X)

        return make_predictions_with_calib(self.task_core.target, X_test, y_classes, classifier_data)




# a list of pairs indicating the slices of the data containing full seizures
# e.g. [(0, 5), (6, 10)] indicates two ranges of seizures
def seizure_ranges_for_latencies(latencies):
    indices = np.where(latencies == 0)[0]

    ranges = []
    for i in range(1, len(indices)):
        ranges.append((indices[i-1], indices[i]))
    ranges.append((indices[-1], len(latencies)))

    return ranges


#generator to iterate over competition mat data
def load_mat_data(data_dir, target, component):
    dir = os.path.join(data_dir, target)
    done = False
    i = 0
    while not done:
        i += 1
        filename = '%s/%s_%s_segment_%04d.mat' % (dir, target, component, i)
        if os.path.exists(filename):
            data = scipy.io.loadmat(filename)
            yield(data)
        else:
            if i == 1:
                raise Exception("file %s not found" % filename)
            done = True


# process all of one type of the competition mat data
# data_type is one of ('ictal', 'interictal', 'test')
def parse_input_data(data_dir, target, data_type, pipeline, gen_ictal=False):
    ictal = data_type == 'preictal'
    interictal = data_type == 'interictal'

    mat_data = load_mat_data(data_dir, target, data_type)

    # for each data point in ictal, interictal and test,
    # generate (X, <y>, <latency>) per channel
    def process_raw_data(mat_data, ispreictal):
        start = time.get_seconds()
        X = []
        y = []
        latencies = []

        prev_data = None
        prev_latency = None
        for segment in mat_data:

            for key in segment.keys():
                if (key.find('segment')>0):
                    keyname = key

            data = segment[keyname]

            # TODO:xf1280@gmail.com
            data = data[0,0]
            datas = data['data']
            sz = datas.shape
            for i in range(drate):
                data = datas[:,i*(sz[1]//drate):(i+1)*(sz[1]//drate)]
                transformed_data = pipeline.apply(data)
                if ispreictal:
                    # this is preictal
                    y.append(1)
                else:
                    # this is interictal
                    y.append(0)

                X.append(transformed_data)

                prev_data = data


        print '(%ds)' % (time.get_seconds() - start)

        X = np.array(X)
        y = np.array(y)
        latencies = np.array(latencies)

        if ictal:
            print 'X', X.shape, 'y', y.shape
            return X, y
        elif interictal:
            print 'X', X.shape, 'y', y.shape
            return X, y
        else:
            print 'X', X.shape
            return X

    data = process_raw_data(mat_data, ispreictal=ictal)
    if len(data) == 2:
        X, y = data
        return {
            'X': X,
            'y': y
        }
    else:
        X = data
        return {
            'X': X
        }


# flatten data down to 2 dimensions for putting through a classifier
def flatten(data):
    if data.ndim > 2:
        return data.reshape((data.shape[0], np.product(data.shape[1:])))
    else:
        return data


# split up ictal and interictal data into training set and cross-validation set
def prepare_training_data(ictal_data, interictal_data, cv_ratio):
    ictal_X, ictal_y = flatten(ictal_data.X), ictal_data.y
    interictal_X, interictal_y = flatten(interictal_data.X), interictal_data.y
    sz = ictal_X.shape
    num = sz[0]
    num2 = (interictal_X.shape)[0]
    sub = random.sample(range(0,interictal_X.shape[0]),min(num*3,num2))
    #sub = random.sample(range(0,interictal_X.shape[0]),num)
    interictal_X = interictal_X[sub,:]
    interictal_y = interictal_y[sub]
    #chop data
    print "chop"
    print interictal_X.shape
    print ictal_X.shape
    # split up data into training set and cross-validation set for both seizure and early sets
    ictal_X_train, ictal_y_train, ictal_X_cv, ictal_y_cv = split_train_random(ictal_X, ictal_y, cv_ratio)
    interictal_X_train, interictal_y_train, interictal_X_cv, interictal_y_cv = split_train_random(interictal_X, interictal_y, cv_ratio)
    print interictal_X_train.shape

    def concat(a, b):
        return np.concatenate((a, b), axis=0)

    X_train = concat(ictal_X_train, interictal_X_train)
    y_train = concat(ictal_y_train, interictal_y_train)
    print X_train.shape
    X_cv = concat(ictal_X_cv, interictal_X_cv)
    y_cv = concat(ictal_y_cv, interictal_y_cv)

    y_classes = np.unique(concat(y_train, y_cv))

    start = time.get_seconds()
    elapsedSecs = time.get_seconds() - start
    print "%ds" % int(elapsedSecs)

    print 'X_train:', np.shape(X_train)
    print 'y_train:', np.shape(y_train)
    print 'X_cv:', np.shape(X_cv)
    print 'y_cv:', np.shape(y_cv)
    print 'y_classes:', y_classes

    return {
        'X_train': X_train,
        'y_train': y_train,
        'X_cv': X_cv,
        'y_cv': y_cv,
        'y_classes': y_classes
    }


# split interictal segments at random for training and cross-validation
def split_train_random(X, y, cv_ratio):
    X_train, X_cv, y_train, y_cv = cross_validation.train_test_split(X, y, test_size=cv_ratio, random_state=0)
    return X_train, y_train, X_cv, y_cv


# split ictal segments for training and cross-validation by taking whole seizures at a time
#def split_train_ictal(X, y,  cv_ratio):
#    #seizure_ranges = seizure_ranges_for_latencies(latencies)
#    #seizure_durations = [r[1] - r[0] for r in seizure_ranges]
#
#    num_seizures = len(seizure_ranges)
#    num_cv_seizures = int(max(1.0, num_seizures * cv_ratio))
#
#    # sort seizures by biggest duration first, then take the middle chunk for cross-validation
#    # and take the left and right chunks for training
#    tagged_durations = zip(range(len(seizure_durations)), seizure_durations)
#    tagged_durations.sort(cmp=lambda x,y: cmp(y[1], x[1]))
#    middle = num_seizures / 2
#    half_cv_seizures = num_cv_seizures / 2
#    start = middle - half_cv_seizures
#    end = start + num_cv_seizures
#
#    chosen = tagged_durations[start:end]
#    chosen.sort(cmp=lambda x,y: cmp(x[0], y[0]))
#    cv_ranges = [seizure_ranges[r[0]] for r in chosen]
#
#    train_ranges = []
#    prev_end = 0
#    for start, end in cv_ranges:
#        train_start = prev_end
#        train_end = start
#
#        if train_start != train_end:
#            train_ranges.append((train_start, train_end))
#
#        prev_end = end
#
#    train_start = prev_end
#    train_end = len(latencies)
#    if train_start != train_end:
#        train_ranges.append((train_start, train_end))
#
#    X_train_chunks = [X[start:end] for start, end in train_ranges]
#    y_train_chunks = [y[start:end] for start, end in train_ranges]
#
#    X_cv_chunks = [X[start:end] for start, end in cv_ranges]
#    y_cv_chunks = [y[start:end] for start, end in cv_ranges]
#
#    X_train = np.concatenate(X_train_chunks)
#    y_train = np.concatenate(y_train_chunks)
#    X_cv = np.concatenate(X_cv_chunks)
#    y_cv = np.concatenate(y_cv_chunks)
#
#    return X_train, y_train, X_cv, y_cv


# train classifier for cross-validation
def train(classifier, X_train, y_train, X_cv, y_cv, y_classes):
    print "Training ..."
    print 'Dim', 'X', np.shape(X_train), 'y', np.shape(y_train), 'X_cv', np.shape(X_cv), 'y_cv', np.shape(y_cv)

    start = time.get_seconds()
    total = y_train.shape[0]


    ictalnum = sum(y_train)
    interictalnum = total - ictalnum
    print ictalnum
    print interictalnum

    weight = np.concatenate((interictalnum/ictalnum*np.ones(ictalnum),np.ones(interictalnum)))
    print weight


    #classifier.fit(X_train, y_train, sample_weight=weight)
    classifier.fit(X_train,y_train)
    print "Scoring..."
    S= score_classifier_auc(classifier, X_cv, y_cv, y_classes)
    score = S

    elapsedSecs = time.get_seconds() - start
    print "t=%ds score=%f" % (int(elapsedSecs), score)
    return score, S


# train classifier for predictions
def train_all_data(classifier, X_train, y_train, X_cv, y_cv):
    print "Training ..."
    X = np.concatenate((X_train, X_cv), axis=0)
    y = np.concatenate((y_train, y_cv), axis=0)
    print 'Dim', np.shape(X), np.shape(y)
    start = time.get_seconds()
    total = (y.shape)[0]
    ictalnum = sum(y)
    interictalnum = total - ictalnum
    print ictalnum
    print interictalnum
    weight = np.concatenate((interictalnum/ictalnum*np.ones(ictalnum),np.ones(interictalnum)))
    print weight
    classifier.fit(X, y, sample_weight= weight)
    #np.set_printoptions(threshold=np.nan)
    elapsedSecs = time.get_seconds() - start
    print "t=%ds" % int(elapsedSecs)







# sub mean divide by standard deviation
def normalize_data(X_train, X_cv):
    scaler = preprocessing.StandardScaler()
    scaler.fit(X_train)

    X_train = scaler.transform(X_train)
    X_cv = scaler.transform(X_cv)

    return X_train, X_cv

# depending on input train either for predictions or for cross-validation
def train_classifier(classifier, data, use_all_data=False, normalize=False, return_data = False):
    X_train = data.X_train
    y_train = data.y_train
    X_cv = data.X_cv
    y_cv = data.y_cv
    if normalize:
        X_train, X_cv = normalize_data(X_train, X_cv)
    if not use_all_data:
        score, S = train(classifier, X_train, y_train, X_cv, y_cv, data.y_classes)
        if return_data:
            return {
                    'y_cv':y_cv.reshape((y_cv.size//drate,drate)).mean(axis=-1),
                    'pred':classifier.predict_proba(X_cv)[:,1].reshape((y_cv.size//drate,drate)).mean(axis=-1)
                    }
        else:
            return {
                'classifier': classifier,
                'score': score,
                'S_auc': S,
            }
    else:
        train_all_data(classifier, X_train, y_train, X_cv, y_cv)
        return {
            'classifier': classifier
        }



# depending on input train either for predictions or for cross-validation
def train_classifier_with_calib(classifier, data, use_all_data=False, normalize=False):
    X_train = data.X_train
    y_train = data.y_train
    X_cv = data.X_cv
    y_cv = data.y_cv
    if normalize:
        X_train, X_cv = normalize_data(X_train, X_cv)
    if not use_all_data:
        ir = IR()
        score, S = train(classifier, X_train, y_train, X_cv, y_cv, data.y_classes)
        predictions_proba = classifier.predict_proba(X_cv)
        proba = predictions_proba[:,1];
        ir.fit_transform(proba,y_cv)
        print proba
        print ir
        return {
            'classifier': classifier,
            'score': score,
            'S_auc': S,
            'IR':ir,
            'prange':[np.amin(proba),np.amax(proba)]
        }
    else:
        train_all_data(classifier, X_train, y_train, X_cv, y_cv)
        return {
            'classifier': classifier
        }

# convert the output of classifier predictions into (Seizure, Early) pair
def translate_prediction(prediction, y_classes):
    #print len(prediction)
    #if len(prediction) == 3:
    #    # S is 1.0 when ictal <=15 or >15
    #    # S is 0.0 when interictal is highest
    #    ictalLTE15, ictalGT15, interictal = prediction
    #    S = ictalLTE15 + ictalGT15
    #    E = ictalLTE15
    #    return S, E
    #elif len(prediction) == 2:
    #    # 1.0 doesn't exist for Patient_4, i.e. there is no late seizure data
    #    if not np.any(y_classes == 1.0):
    #        ictalLTE15, interictal = prediction
    #        S = ictalLTE15
    #        E = ictalLTE15
    #        # y[i] = 0 # ictal <= 15
    #        # y[i] = 1 # ictal > 15
    #        # y[i] = 2 # interictal
    #        return S, E
    #    else:
    #        raise NotImplementedError()
    #else:
    #    raise NotImplementedError()

    S = prediction[1]
    return S

# use the classifier and make predictions on the test data
def make_predictions(target, X_test, y_classes, classifier_data):
    classifier = classifier_data.classifier
    predictions_proba = classifier.predict_proba(X_test)
    #np.set_printoptions(threshold=np.nan)
    proba = predictions_proba[:,1];
    print proba
    proba = proba.reshape((proba.size//drate,drate))
    print proba
    proba = proba.mean(axis=-1)
    lines = []
    print proba
    #proba = proba - proba.mean(axis=0)+0.5
    #for i in range(len(predictions_proba)):
    for i in range(len(proba)):
        #p = [proba[i],0]
        #S = translate_prediction(p, y_classes)
        S = proba[i]
        lines.append('%s_test_segment_%04d.mat,%.15f' % ( target, i+1, S))

    return {
        'data': '\n'.join(lines)
    }



# use the classifier and make predictions on the test data
def make_predictions_with_calib(target, X_test, y_classes, classifier_data):
    classifier = classifier_data.classifier
    ir = classifier_data.IR
    predictions_proba = classifier.predict_proba(X_test)
    #np.set_printoptions(threshold=np.nan)
    proba = predictions_proba[:,1];
    print proba
    print "transformed"
    lowValue = classifier_data.prange[0]
    highValue = classifier_data.prange[1]
    lowIndices = proba <= lowValue
    highIndices = proba >= highValue
    proba[lowIndices] = lowValue + 0.001
    proba[highIndices] = highValue - 0.001
    proba = ir.transform(proba)
    print proba
    proba = proba.reshape((proba.size//drate,drate))
    print proba
    proba = proba.mean(axis=-1)
    lines = []
    print proba


    #proba = proba - proba.mean(axis=0)+0.5
    #for i in range(len(predictions_proba)):
    for i in range(len(proba)):
        #p = [proba[i],0]
        #S = translate_prediction(p, y_classes)
        S = proba[i]
        lines.append('%s_test_segment_%04d.mat,%.15f' % ( target, i+1, S))

    return {
        'data': '\n'.join(lines)
    }

# the scoring mechanism used by the competition leaderboard
def score_classifier_auc(classifier, X_cv, y_cv, y_classes):
    predictions = classifier.predict_proba(X_cv)
    S_predictions = []
    S_y_cv =  y_cv

    for i in range(len(predictions)):
        p = predictions[i]
        S= translate_prediction(p, y_classes)
        S_predictions.append(S)

    fpr, tpr, thresholds = roc_curve(S_y_cv, S_predictions)
    S_roc_auc = auc(fpr, tpr)
    return S_roc_auc

