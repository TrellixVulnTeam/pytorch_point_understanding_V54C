import sys
import os
import shutil
import tqdm
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.autograd
from torch.autograd import Variable
from sklearn.metrics import confusion_matrix

# Must be after to use locally modified torchvision libs
import torchvision
import torchvision.transforms

from .models import resnet_dilated
from .models import partsnet
from .datasets.pascal_voc import PascalVOCSegmentation
from .transforms import (ComposeJoint,
                         RandomHorizontalFlipJoint,
                         RandomCropJoint)

path = os.path.dirname(os.path.realpath(__file__))
patharr = path.split(os.sep)
home_dir = os.path.join(
    '/', *patharr[:patharr.index('object_part_segmentation') + 1])
vision_dir = os.path.join(home_dir, 'vision')
dataset_dir = os.path.join(home_dir, 'datasets')
sys.path.insert(0, home_dir)
sys.path.insert(0, vision_dir)


def poly_lr_scheduler(optimizer, init_lr, iteration, lr_decay_iter=1,
                      max_iter=100, power=0.9):
    """Polynomial decay of learning rate
        :param init_lr is base learning rate
        :param iteration is a current iteration
        :param lr_decay_iter how frequently decay occurs, default is 1
        :param max_iter is number of maximum iterations
        :param power is a polymomial power
        Credit @trypag
        https://discuss.pytorch.org/t/solved-learning-rate-decay/6825/5
    """
    if iteration % lr_decay_iter or iteration > max_iter:
        return optimizer

    lr = init_lr * (1 - iteration / max_iter)**power
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

    return lr


def get_training_loaders(dataset_dir, network_dims, batch_size=8,
                         num_workers=4, mask_type="consensus",
                         which='binary'): 
    '''Returns loaders for the training set.
        args:
            :param ``network_dims``: ``dict`` which will
            store the output label splits
            :param ``dataset_dir``: str indicating the directory
            inwhich the Pascal VOC dataset is stored
            ... etc.
            :param ``mask_type``:
            :param ``which``: one of 'binary,' 'merged', or 'sparse'
            'binary': for each class: object or part
            'merged': for each class: object or one of k "super-parts"
            'sparse': for each calss: object or one of N parts
    '''

    assert isinstance(network_dims, dict)

    train_transform = ComposeJoint(
        [
            RandomHorizontalFlipJoint(),
            RandomCropJoint(crop_size=(513, 513), pad_values=[
                0, 255, -1]),
            [torchvision.transforms.ToTensor(), None, None],
            [torchvision.transforms.Normalize(
                (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)), None, None],
            # convert labels to tensors
            [None, torchvision.transforms.Lambda(
                lambda x: torch.from_numpy(np.asarray(x)).long()),
             # Point Labels
             torchvision.transforms.Lambda(
                 lambda x: torch.from_numpy(np.asarray(x)).long())]
        ])

    trainset = PascalVOCSegmentation(dataset_dir,
                                     network_dims=network_dims,
                                     download=False,
                                     joint_transform=train_transform,
                                     mask_type=mask_type,
                                     which=which)

    trainloader = torch.utils.data.DataLoader(
        trainset, batch_size=batch_size,
        shuffle=True, num_workers=num_workers, drop_last=True)

    valid_transform = ComposeJoint(
        [
            [torchvision.transforms.ToTensor(), None, None],
            [torchvision.transforms.Normalize(
                (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)), None, None],
            # convert labels to tensors
            [None, torchvision.transforms.Lambda(
                lambda x: torch.from_numpy(np.asarray(x)).long()),
             # Point Labels
             torchvision.transforms.Lambda(
                 lambda x: torch.from_numpy(np.asarray(x)).long())]
        ])

    valset = PascalVOCSegmentation(dataset_dir,
                                   network_dims={},
                                   train=False,
                                   download=False,
                                   joint_transform=valid_transform,
                                   mask_type=mask_type,
                                   which=which)

    valset_loader = torch.utils.data.DataLoader(valset, batch_size=1,
                                                shuffle=False, num_workers=2)

    return (trainloader, trainset), (valset_loader, valset)


def flatten_logits(logits, number_of_classes):
    """Flattens the logits batch except for the logits dimension"""
    logits_permuted = logits.permute(0, 2, 3, 1)
    logits_permuted_cont = logits_permuted.contiguous()
    logits_flatten = logits_permuted_cont.view(-1, number_of_classes)
    return logits_flatten


def get_valid_logits(logits, index, number_of_classes):
    ''' processes predictions based on the valid indices (selected
    from annotations)
    '''
    logits_flatten = flatten_logits(
        logits, number_of_classes=number_of_classes)
    return torch.index_select(logits_flatten, 0, index)


def flatten_annotations(annotations):
    return annotations.view(-1)


def get_valid_annotations_index(flatten_annotations, mask_out_value=255):
    nonz = torch.nonzero((flatten_annotations != mask_out_value))
    if len(nonz) == 0:
        return torch.Tensor([])
    return torch.squeeze(nonz, 1)


def get_valid_annos(anno, mask_out_value):
    ''' selects labels not masked out
        returns a flattened tensor of annotations and the indices which are
        valid
    '''
    anno_flatten = flatten_annotations(anno)
    index = get_valid_annotations_index(
        anno_flatten, mask_out_value=mask_out_value)
    anno_flatten_valid = torch.index_select(anno_flatten, 0, index)
    return anno_flatten_valid, index


def numpyify_logits_and_annotations(logits, anno, flatten=True):
    ''' process logits and annotations for input into
    confusion matrix function.

    args::
        ``logits``: network predictoins
        ``anno``: ground truth annotations

    returns::
        flattened predictions, flattened annotations
    '''
    # First we do argmax on gpu and then transfer it to cpu
    _logits = logits.data
    _, prediction = _logits.max(1)
    prediction = prediction.squeeze(1)
    prediction_np = prediction.cpu().numpy()
    anno_np = anno.numpy()
    if flatten:
        return prediction_np.flatten(), anno_np.flatten()
    else:
        return prediction_np, anno_np


def outputs_tonp_gt(logits, anno, flatten=True):
    ''' process logits and annotations for input into
    confusion matrix function.

    args::
        ``logits``: network predictoins
        ``anno``: ground truth annotations

    returns::
        flattened predictions, flattened annotations
    '''
    def to_pair(ind, num_to_aggregate):
        '''Get the indices for corresponding obj-part pairs.'''
        if ind > num_to_aggregate:
            return [ind - num_to_aggregate, ind]
        else:
            return [ind, ind + num_to_aggregate]

    _logits = logits.data.cpu()
    anno_np = anno.numpy()
    predictions = np.zeros_like(anno_np)
    num_to_aggregate = 20  # Wow this is hacky. TODO change
    # for index, anno_ind in np.ndenumerate(anno_np):
    for index in zip(*np.where(np.logical_and(anno_np > 0, anno_np != 255))):
        anno_ind = anno_np[index]
        batch_ind = index[0]
        i = index[1]
        j = index[2]
        channel_indices = to_pair(anno_ind, num_to_aggregate)
        aided_prediction = channel_indices[np.argmax(
            [_logits[batch_ind, ci, i, j] for ci in channel_indices])]

        predictions[batch_ind, i, j] = aided_prediction
    if flatten:
        return predictions.flatten(), anno_np.flatten()
    else:
        return predictions, anno_np


def compress_objpart_logits(logits, anno):
    ''' Reduce N x 41 tensor ``logits`` to an N x 2 tensor ``compressed``,
        where ``compressed``[0] => "object" and ``compressed``[1] => part
        (generic).
    args::
        ``logits``: network predictions => 2D tensor of shape (N, 41)
        ``anno``: ground truth annotations => 1D tensor of length N

    returns::
        compressed tensor of shape (N, 2)
    '''
    anno = anno.data.cpu()

    def to_pair(ind, num_to_aggregate):
        '''Get the indices for corresponding obj-part pairs.'''
        if ind > num_to_aggregate:
            return 1, [ind - num_to_aggregate, ind]
        else:
            return 0, [ind, ind + num_to_aggregate]
    indices = []
    new_anno = []
    for i, label in enumerate(anno):
        new_label, pair = to_pair(label, 20)
        indices.append(pair)
        new_anno.append(new_label)

    new_anno = Variable(torch.LongTensor(new_anno).cuda())
    indices = Variable(torch.LongTensor(indices).cuda())
    compressed_logits = torch.gather(logits, 1, indices)
    return compressed_logits, new_anno


def get_iou(confusion_matrix):
    '''

    Used for computing the intersection over union metric
    using a confusion matrix. Pads unseen labels (union)
    with epsilon to avoid nan.
    Returns a vector of length |labels| with
    the IoU for each class in its appropriate
    place.

    '''
    intersection = np.diag(confusion_matrix)
    gt_set = confusion_matrix.sum(axis=1)
    predicted_set = confusion_matrix.sum(axis=0)
    union = gt_set + predicted_set - intersection
    # Ensure no divide by 1 errors
    eps = 1  # 1e-5
    union[union == 0] = eps
    iou = intersection / union.astype(np.float32)
    return iou


def get_precision_recall(confusion_matrix):
    ''' Returns the class-wise precision and recall given a
        confusion matrix.
        Note that this defaults to 0 to avoids divide by zero errors.
    '''
    intersection = np.diag(confusion_matrix)
    gt_set = confusion_matrix.sum(axis=1)
    predicted_set = confusion_matrix.sum(axis=0)
    precision = intersection / \
        np.array([np.max([pred, 1.0]) for pred in predicted_set]).astype(
            np.float32)
    recall = intersection / \
        np.array([np.max([gt, 1.0]) for gt in gt_set]).astype(np.float32)
    return precision, recall


def validate_batch(
        objpart_dat,
        semantic_dat,
        overall_part_confusion_matrix,
        overall_semantic_confusion_matrix,
        labels,
        writer=None,
        index=0):
    ''' Computes the running IoU for the semantic and object-part tasks.
        args::
            :param (objpart_logits, objpart_anno): prediction, ground_truth
                    tensors for the object-part inference task
            :param (semantic_logits, semantic_anno): ditto for the semantic
            segmentation task
            :param overal_semantic_confusion_matrix: None or tensor of length
                                    |segmentation classes|. Total confusion
                                    matrix for semantic segmentation task
                                    for this epoch.
            :param overal_part_confusion_matrix: None or tensor of length
                                    |segmentation classes|. Total confusion
                                    matrix for object-part inference task
                                    for this epoch.

    '''
    (objpart_logits, objpart_anno) = objpart_dat
    (semantic_logits, semantic_anno) = semantic_dat
    objpart_labels, semantic_labels = labels
    semantic_prediction_np, semantic_anno_np = numpyify_logits_and_annotations(
        semantic_logits, semantic_anno)
    # objpart_prediction_np, objpart_anno_np = numpyify_logits_and_annotations(
    #     objpart_logits, objpart_anno)
    objpart_prediction_np, objpart_anno_np = outputs_tonp_gt(
        objpart_logits, objpart_anno)

    no_parts = [0, 4, 9, 11, 18, 20, 24, 29, 31, 38, 40]
    # Make sure to ignore all background class values
    objpart_anno_np[objpart_anno_np == 0] = -1
    # objpart_prediction_np[objpart_anno_np == 0] = -1

    # Mask-out value is ignored by default in the sklearn
    # read sources to see how that was handled
    current_semantic_confusion_matrix = confusion_matrix(
        y_true=semantic_anno_np,
        y_pred=semantic_prediction_np,
        labels=semantic_labels)

    current_objpart_confusion_matrix = confusion_matrix(
        y_true=objpart_anno_np, y_pred=objpart_prediction_np,
        labels=objpart_labels)

    if overall_semantic_confusion_matrix is None:
        overall_semantic_confusion_matrix = current_semantic_confusion_matrix
    else:
        overall_semantic_confusion_matrix += current_semantic_confusion_matrix

    if overall_part_confusion_matrix is None:
        overall_part_confusion_matrix = current_objpart_confusion_matrix
    else:
        overall_part_confusion_matrix += current_objpart_confusion_matrix

    semantic_IoU = get_iou(
        current_semantic_confusion_matrix)
    semantic_mIoU = np.mean(semantic_IoU)

    objpart_prec, objpart_rec = get_precision_recall(
        current_objpart_confusion_matrix)
    objpart_mPrec = np.mean(
        [prec for i, prec in enumerate(objpart_prec) if i not in no_parts])
    objpart_mRec = np.mean(
        [rec for i, rec in enumerate(objpart_rec) if i not in no_parts])

    if writer is not None:
        # writer.add_scalar('data/objpart_mIoU', objpart_mIoU, index)
        writer.add_scalar('data/objpart_mPrec', objpart_mPrec, index)
        writer.add_scalar('data/objpart_mRec', objpart_mRec, index)
        writer.add_scalar('data/semantic_mIoU', semantic_mIoU, index)
        writer.add_scalars('data/semantic_IoUs',
                           {'cls ' + str(i): v for i,
                            v in enumerate(semantic_IoU)},
                           index)
        # writer.add_scalars(
        #     'data/part_IoUs', {'cls ' + str(i): v for i, v
        # in enumerate(objpart_IoU)}, index)
        writer.add_scalars('data/part_prec',
                           {'cls ' + str(i): v for i,
                            v in enumerate(objpart_prec)},
                           index)
        writer.add_scalars('data/part_rec',
                           {'cls ' + str(i): v for i,
                            v in enumerate(objpart_rec)},
                           index)

    return ((objpart_mPrec, objpart_mRec),
            semantic_mIoU, overall_part_confusion_matrix,
            overall_semantic_confusion_matrix)
    # return objpart_mIoU, semantic_mIoU, overall_part_confusion_matrix,
    # overall_semantic_confusion_matrix


def save_checkpoint(state, is_best, folder='models',
                    filename='checkpoint.pth.tar'):
    ''' Saves a model
        args::
            :param ``staet``: dictionary containing training data.
            :param ``is_best``: boolean determining if this represents
                            the best-trained model of this session
            :param ``folder``: relative path to folder in which to save
            checkpoint
            :param ``filename``: name of the checkpoint file

        additionally copies to "[architecture]" + "_model_best.pth.tar"
        if is_best.
    '''
    torch.save(state, os.path.join(folder, filename))
    if is_best:
        shutil.copyfile(
            os.path.join(
                folder, filename), os.path.join(
                    folder, state['arch'] + '_model_best.pth.tar'))


def load_checkpoint(load_path, fcn, optimizer):
    ''' Loads network parameters (and optimizer params) from a checkpoint file.
        args::
            :param ``load_path``: string path to checkpoint file.
            :param ``fcn``: torch.nn network
            :param ``optimizer``: duh
        returns the starting epoch and best scores
    '''
    if os.path.isfile(load_path):
        print("=> loading checkpoint '{}'".format(load_path))
        checkpoint = torch.load(load_path)
        start_epoch = checkpoint['epoch']
        best_semantic_val_score = checkpoint['best_semantic_mIoU']
        best_objpart_val_score = checkpoint['best_objpart_mIoU']
        fcn.load_state_dict(checkpoint['state_dict'])
        if optimizer is not None:
            optimizer.load_state_dict(checkpoint['optimizer'])

        print("=> loaded checkpoint '{}' (epoch {})"
              .format(load_path, checkpoint['epoch']))
    else:
        raise RuntimeError("{} does not exist.".format(load_path))

    return (start_epoch, best_semantic_val_score,
            best_objpart_val_score)


def get_ae_network_and_optimizer(
        number_of_classes,
        to_aggregate,
        load_from_local=False,
        model_path=None,
        train_params=None):
    ''' Gets the network and corresponding optimizer.
        args::
            # of semantic segmentaton classes (final output)
            :param ``number_of_classes``:
            :param ``to_aggregate``: # of classes which have corresponding
            object and part channels.
            :param ``load_from_local``: boolean variable to determine whether
            to load parameters from a local checkpoint file
            :param ``model_path``: required if ``load_from_local`` is ``True``,
            String path to checkpoint file

        returns a net, optimizer, (criteria), and best scores

        TODO: make this flexible. This is really sloppy.

    '''
    fcn = partsnet.Resnet34_8s(
        num_classes=number_of_classes,
        num_to_aggregate=to_aggregate,
        part_task=True)
    optimizer = optim.Adam(fcn.parameters(), lr=0.0001, weight_decay=0.0001)
    if load_from_local:
        (start_epoch, best_semantic_val_score,
         best_objpart_val_score) = load_checkpoint(
             model_path, fcn, optimizer)
    else:
        start_epoch = 0
        best_semantic_val_score, best_objpart_val_score = 0.0, 0.0
    fcn.cuda()
    fcn.train()

    semantic_criterion = nn.CrossEntropyLoss(size_average=False).cuda()
    objpart_criterion = nn.CrossEntropyLoss(size_average=False).cuda()

    _to_update = {
        'semantic_criterion': semantic_criterion,
        'objpart_criterion': objpart_criterion,
        'best_objpart_val_score': best_objpart_val_score,
        'best_semantic_val_score': best_semantic_val_score,
        'start_epoch': start_epoch
    }
    train_params.update(_to_update)

    # optimizer = optim.Adam(fcn.parameters(), lr=0.0001, weight_decay=0.0001)
    return fcn, optimizer


def get_network_and_optimizer(
        network_dims,
        load_from_local=False,
        model_path=None,
        train_params=None):
    ''' Gets the network and corresponding optimizer.
        args::
            # of semantic segmentaton classes (final output)
            :param ``number_of_classes``:
            :param ``to_aggregate``: # of classes which have corresponding
            object and part channels.
            :param ``load_from_local``: boolean variable to determine whether
            to load parameters from a local checkpoint file
            :param ``model_path``: required if ``load_from_local`` is ``True``,
            String path to checkpoint file

        returns a net, optimizer, (criteria), and best scores

        TODO: make this flexible. This is really sloppy.

    '''
    fcn = resnet_dilated.Resnet34_8s(
        output_dims=network_dims,
        part_task=True)

    optimizer = optim.Adam(fcn.parameters(), lr=0.0001, weight_decay=0.0001)
    if load_from_local:
        (start_epoch, best_semantic_val_score,
         best_objpart_val_score) = load_checkpoint(
             model_path, fcn, optimizer)
    else:
        start_epoch = 0
        best_semantic_val_score, best_objpart_val_score = 0.0, 0.0
    fcn.cuda()
    fcn.train()

    semantic_criterion = nn.CrossEntropyLoss(size_average=False).cuda()
    objpart_criterion = nn.CrossEntropyLoss(size_average=False).cuda()

    _to_update = {
        'semantic_criterion': semantic_criterion,
        'objpart_criterion': objpart_criterion,
        'best_objpart_val_score': best_objpart_val_score,
        'best_semantic_val_score': best_semantic_val_score,
        'start_epoch': start_epoch
    }
    train_params.update(_to_update)

    # optimizer = optim.Adam(fcn.parameters(), lr=0.0001, weight_decay=0.0001)
    return fcn, optimizer


def get_cmap():
    import json
    fname = os.path.join(
        os.path.dirname(
            os.path.realpath(__file__)),
        'colortable.json')
    with open(fname, 'r') as f:
        cmap = json.load(f)
    return cmap


def validate_and_output_images(net, loader, which='semantic', alpha=0.6):
    ''' Computes mIoU for``net`` over the a set.
        args:: :param ``net``: network (in this case resnet34_8s_dilated
            :param ``loader``: dataloader (in this case, validation set loader)

        returns the mIoU (ignoring classes where no parts were annotated) for
        the semantic segmentation task and object-part inference task.
      0: "background",
      1: "aeroplane",
      2: "bicycle",
      3: "bird",
      4: "boat",
      5: "bottle",
      6: "bus",
      7: "car",
      8: "cat",
      9: "chair",
      10: "cow",
      11: "diningtable",
      12: "dog",
      13: "horse",
      14: "motorbike",
      15: "person",
      16: "pottedplant",
      17: "sheep",
      18: "sofa",
      19: "train",
      20: "tvmonitor"
      21: "aeroplane_part",
      22: "bicycle_part",
      23: "bird_part",
      24: "boat_part",
      25: "bottle_part",
      26: "bus_part",
      27: "car_part",
      28: "cat_part",
      29: "chair_part",
      30: "cow_part",
      31: "diningtable_part",
      32: "dog_part",
      33: "horse_part",
      34: "motorbike_part",
      35: "person_part",
      36: "pottedplant_part",
      37: "sheep_part",
      38: "sofa_part",
      39: "train_part",
      40: "tvmonitor_part"

    '''
    from PIL import Image
    net.eval()
    # hardcoded in for the object-part infernce
    # TODO: change to be flexible/architecture-dependent
    # no_parts = [0, 4, 9, 11, 18, 20, 24, 29, 31, 38, 40]
    # objpart_labels, semantic_labels = labels
    cmap = get_cmap()

    # valset_loader
    i = 0
    for image, semantic_anno, objpart_anno in tqdm.tqdm(loader):
        img = Variable(image.cuda())
        objpart_logits, semantic_logits = net(img)

        # First we do argmax on gpu and then transfer it to cpu
        if which == 'semantic':
            prediction, anno = numpyify_logits_and_annotations(
                semantic_logits, semantic_anno, flatten=False)
        elif which == 'separated':
            prediction, anno = numpyify_logits_and_annotations(
                objpart_logits, objpart_anno, flatten=False)
        elif which == 'objpart':
            # prediction, anno = outputs_tonp_gt(
            #     objpart_logits, objpart_anno,semantic_anno, flatten=False)
            prediction, anno = outputs_tonp_gt(
                objpart_logits, semantic_anno, flatten=False)
            prediction[np.logical_and(
                prediction > 0, prediction < 21)] = 1  # object
            prediction[prediction > 20] = 2  # part
        else:
            raise ValueError(
                '"which" value of {} not valid. Must be one of "semantic",'
                '"separated", or'
                '"objpart"'.format(which))

        image_copy = np.array(image).squeeze(0).transpose(1, 2, 0)
        image_copy = image_copy.astype(np.float32)
        image_copy -= image_copy.min()
        image_copy /= image_copy.max()
        # image_copy*=255
        prediction = prediction.squeeze(0)
        cmask = np.zeros_like(image_copy, dtype=np.float32)
        classes = np.unique(prediction)
        # sz = prediction.size
        for cls in classes:
            if cls <= 0:
                continue
            ind = prediction == cls
            cmask[ind, :] = cmap[cls]

        cmask = cmask.astype(np.float32) / cmask.max()
        ind = prediction > 0
        image_copy[ind] = image_copy[ind] * \
            (1.0 - alpha) + cmask[ind] * (float(alpha))
        image_copy = image_copy - image_copy.min()
        image_copy = image_copy / np.max(image_copy)
        image_copy = image_copy * 255
        image_copy = image_copy.astype(np.uint8)
        image_copy = Image.fromarray(image_copy)
        image_copy.save("predictions/validation_{}_{}.png".format(which, i))
        i += 1
        image_copy.close()
        # hxwx(rgb)