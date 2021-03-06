# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

import os
import time
from PyQt5.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout
import yaml
import cv2
import numpy as np
import paddle
from benchmark_utils import PaddleInferBenchmark
from preprocess import preprocess, NormalizeImage, Permute
from mot_preprocess import LetterBoxResize

from tracker import JDETracker
from ppdet.modeling.mot import visualization as mot_vis
from ppdet.modeling.mot.utils import Timer as MOTTimer

from paddle.inference import Config
from paddle.inference import create_predictor
from utils import argsparser, Timer, get_current_memory_mb
from infer import get_test_images, print_arguments

# from video import MyProgressBar

# Global dictionary
MOT_SUPPORT_MODELS = {
    'JDE',
    'FairMOT',
}

# gobal
# is_run = False

class MOT_Detector(object):
    """
    Args:
        pred_config (object): config of model, defined by `Config(model_dir)`
        model_dir (str): root path of model.pdiparams, model.pdmodel and infer_cfg.yml
        use_gpu (bool): whether use gpu
        run_mode (str): mode of running(fluid/trt_fp32/trt_fp16)
        batch_size (int): size of pre batch in inference
        trt_min_shape (int): min shape for dynamic shape in trt
        trt_max_shape (int): max shape for dynamic shape in trt
        trt_opt_shape (int): opt shape for dynamic shape in trt
        trt_calib_mode (bool): If the model is produced by TRT offline quantitative
            calibration, trt_calib_mode need to set True
        cpu_threads (int): cpu threads
        enable_mkldnn (bool): whether to open MKLDNN 
    """

    def __init__(self,
                 pred_config,
                 model_dir,
                 use_gpu=False,
                 run_mode='fluid',
                 batch_size=1,
                 trt_min_shape=1,
                 trt_max_shape=1088,
                 trt_opt_shape=608,
                 trt_calib_mode=False,
                 cpu_threads=1,
                 enable_mkldnn=False):
        self.pred_config = pred_config
        self.predictor, self.config = load_predictor(
            model_dir,
            run_mode=run_mode,
            batch_size=batch_size,
            min_subgraph_size=self.pred_config.min_subgraph_size,
            use_gpu=use_gpu,
            use_dynamic_shape=self.pred_config.use_dynamic_shape,
            trt_min_shape=trt_min_shape,
            trt_max_shape=trt_max_shape,
            trt_opt_shape=trt_opt_shape,
            trt_calib_mode=trt_calib_mode,
            cpu_threads=cpu_threads,
            enable_mkldnn=enable_mkldnn)
        self.det_times = Timer()
        self.cpu_mem, self.gpu_mem, self.gpu_util = 0, 0, 0
        self.tracker = JDETracker()

    def preprocess(self, im):
        preprocess_ops = []
        for op_info in self.pred_config.preprocess_infos:
            new_op_info = op_info.copy()
            op_type = new_op_info.pop('type')
            preprocess_ops.append(eval(op_type)(**new_op_info))
        im, im_info = preprocess(im, preprocess_ops)
        inputs = create_inputs(im, im_info)
        return inputs

    def postprocess(self, pred_dets, pred_embs, threshold):
        online_targets = self.tracker.update(pred_dets, pred_embs)
        online_tlwhs, online_ids = [], []
        online_scores = []
        for t in online_targets:
            tlwh = t.tlwh
            tid = t.track_id
            tscore = t.score
            if tscore < threshold: continue
            vertical = tlwh[2] / tlwh[3] > 1.6
            if tlwh[2] * tlwh[3] > self.tracker.min_box_area and not vertical:
                online_tlwhs.append(tlwh)
                online_ids.append(tid)
                online_scores.append(tscore)
        return online_tlwhs, online_scores, online_ids

    def predict(self, image, threshold=0.5, repeats=1):
        '''
        Args:
            image (dict): dict(['image', 'im_shape', 'scale_factor'])
            threshold (float): threshold of predicted box' score
        Returns:
            online_tlwhs, online_ids (np.ndarray)
        '''
        self.det_times.preprocess_time_s.start()
        inputs = self.preprocess(image)
        self.det_times.preprocess_time_s.end()
        pred_dets, pred_embs = None, None
        input_names = self.predictor.get_input_names()
        for i in range(len(input_names)):
            input_tensor = self.predictor.get_input_handle(input_names[i])
            input_tensor.copy_from_cpu(inputs[input_names[i]])

        self.det_times.inference_time_s.start()
        for i in range(repeats):
            self.predictor.run()
            output_names = self.predictor.get_output_names()
            boxes_tensor = self.predictor.get_output_handle(output_names[0])
            pred_dets = boxes_tensor.copy_to_cpu()
            embs_tensor = self.predictor.get_output_handle(output_names[1])
            pred_embs = embs_tensor.copy_to_cpu()

        self.det_times.inference_time_s.end(repeats=repeats)

        self.det_times.postprocess_time_s.start()
        online_tlwhs, online_scores, online_ids = self.postprocess(
            pred_dets, pred_embs, threshold)
        self.det_times.postprocess_time_s.end()
        self.det_times.img_num += 1
        return online_tlwhs, online_scores, online_ids


def create_inputs(im, im_info):
    """generate input for different model type
    Args:
        im (np.ndarray): image (np.ndarray)
        im_info (dict): info of image
        model_arch (str): model type
    Returns:
        inputs (dict): input of model
    """
    inputs = {}
    inputs['image'] = np.array((im, )).astype('float32')
    inputs['im_shape'] = np.array((im_info['im_shape'], )).astype('float32')
    inputs['scale_factor'] = np.array(
        (im_info['scale_factor'], )).astype('float32')
    return inputs


class PredictConfig_MOT():
    """set config of preprocess, postprocess and visualize
    Args:
        model_dir (str): root path of model.yml
    """

    def __init__(self, model_dir):
        # parsing Yaml config for Preprocess
        deploy_file = os.path.join(model_dir, 'infer_cfg.yml')
        with open(deploy_file) as f:
            yml_conf = yaml.safe_load(f)
        self.check_model(yml_conf)
        self.arch = yml_conf['arch']
        self.preprocess_infos = yml_conf['Preprocess']
        self.min_subgraph_size = yml_conf['min_subgraph_size']
        self.labels = yml_conf['label_list']
        self.mask = False
        self.use_dynamic_shape = yml_conf['use_dynamic_shape']
        if 'mask' in yml_conf:
            self.mask = yml_conf['mask']
        self.print_config()

    def check_model(self, yml_conf):
        """
        Raises:
            ValueError: loaded model not in supported model type 
        """
        for support_model in MOT_SUPPORT_MODELS:
            if support_model in yml_conf['arch']:
                return True
        raise ValueError("Unsupported arch: {}, expect {}".format(yml_conf[
            'arch'], MOT_SUPPORT_MODELS))

    def print_config(self):
        print('-----------  Model Configuration -----------')
        print('%s: %s' % ('Model Arch', self.arch))
        print('%s: ' % ('Transform Order'))
        for op_info in self.preprocess_infos:
            print('--%s: %s' % ('transform op', op_info['type']))
        print('--------------------------------------------')


def load_predictor(model_dir,
                   run_mode='fluid',
                   batch_size=1,
                   use_gpu=False,
                   min_subgraph_size=3,
                   use_dynamic_shape=False,
                   trt_min_shape=1,
                   trt_max_shape=1088,
                   trt_opt_shape=608,
                   trt_calib_mode=False,
                   cpu_threads=1,
                   enable_mkldnn=False):
    """set AnalysisConfig, generate AnalysisPredictor
    Args:
        model_dir (str): root path of __model__ and __params__
        run_mode (str): mode of running(fluid/trt_fp32/trt_fp16/trt_int8)
        batch_size (int): size of pre batch in inference
        use_gpu (bool): whether use gpu
        use_dynamic_shape (bool): use dynamic shape or not
        trt_min_shape (int): min shape for dynamic shape in trt
        trt_max_shape (int): max shape for dynamic shape in trt
        trt_opt_shape (int): opt shape for dynamic shape in trt
        trt_calib_mode (bool): If the model is produced by TRT offline quantitative
            calibration, trt_calib_mode need to set True
        cpu_threads (int): cpu threads
        enable_mkldnn (bool): whether to open MKLDNN 
    Returns:
        predictor (PaddlePredictor): AnalysisPredictor
    Raises:
        ValueError: predict by TensorRT need use_gpu == True.
    """
    if not use_gpu and not run_mode == 'fluid':
        raise ValueError(
            "Predict by TensorRT mode: {}, expect use_gpu==True, but use_gpu == {}"
            .format(run_mode, use_gpu))
    config = Config(
        os.path.join(model_dir, 'model.pdmodel'),
        os.path.join(model_dir, 'model.pdiparams'))
    precision_map = {
        'trt_int8': Config.Precision.Int8,
        'trt_fp32': Config.Precision.Float32,
        'trt_fp16': Config.Precision.Half
    }
    if use_gpu:
        # initial GPU memory(M), device ID
        config.enable_use_gpu(200, 0)
        # optimize graph and fuse op
        config.switch_ir_optim(True)
    else:
        config.disable_gpu()
        config.set_cpu_math_library_num_threads(cpu_threads)
        if enable_mkldnn:
            try:
                # cache 10 different shapes for mkldnn to avoid memory leak
                config.set_mkldnn_cache_capacity(10)
                config.enable_mkldnn()
            except Exception as e:
                print(
                    "The current environment does not support `mkldnn`, so disable mkldnn."
                )
                pass

    if run_mode in precision_map.keys():
        config.enable_tensorrt_engine(
            workspace_size=1 << 10,
            max_batch_size=batch_size,
            min_subgraph_size=min_subgraph_size,
            precision_mode=precision_map[run_mode],
            use_static=False,
            use_calib_mode=trt_calib_mode)

        if use_dynamic_shape:
            min_input_shape = {'image': [1, 3, trt_min_shape, trt_min_shape]}
            max_input_shape = {'image': [1, 3, trt_max_shape, trt_max_shape]}
            opt_input_shape = {'image': [1, 3, trt_opt_shape, trt_opt_shape]}
            config.set_trt_dynamic_shape_info(min_input_shape, max_input_shape,
                                              opt_input_shape)
            print('trt set dynamic shape done!')

    # disable print log when predict
    config.disable_glog_info()
    # enable shared memory
    config.enable_memory_optim()
    # disable feed, fetch OP, needed by zero_copy_run
    config.switch_use_feed_fetch_ops(False)
    predictor = create_predictor(config)
    return predictor, config


def write_mot_results(filename, results, data_type='mot'):
    if data_type in ['mot', 'mcmot', 'lab']:
        save_format = '{frame},{id},{x1},{y1},{w},{h},{score},-1,-1,-1\n'
    elif data_type == 'kitti':
        save_format = '{frame} {id} pedestrian 0 0 -10 {x1} {y1} {x2} {y2} -10 -10 -10 -1000 -1000 -1000 -10\n'
    else:
        raise ValueError(data_type)

    with open(filename, 'w') as f:
        for frame_id, tlwhs, tscores, track_ids in results:
            if data_type == 'kitti':
                frame_id -= 1
            for tlwh, score, track_id in zip(tlwhs, tscores, track_ids):
                if track_id < 0:
                    continue
                x1, y1, w, h = tlwh
                x2, y2 = x1 + w, y1 + h
                line = save_format.format(
                    frame=frame_id,
                    id=track_id,
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    w=w,
                    h=h,
                    score=score)
                f.write(line)


def predict_video(detector, camera_id):
    if camera_id != -1:
        capture = cv2.VideoCapture(camera_id)
        video_name = "mot_output.mp4"
    else:
        capture = cv2.VideoCapture(FLAGS.video_file)
        video_name = os.path.split(FLAGS.video_file)[-1]
    fps = 30
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))


    #new
    progressBar = MyProgressBar()
    per = frame_count / 100;

    # print('frame_count', frame_count)

    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # yapf: disable 
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    # yapf: enable
    if not os.path.exists(FLAGS.output_dir):
        os.makedirs(FLAGS.output_dir)
    out_path = os.path.join(FLAGS.output_dir, video_name)
    writer = cv2.VideoWriter(out_path, fourcc, fps, (width, height))
    frame_id = 0
    timer = MOTTimer()
    results = []

    
    while (is_run):
        #new
        progressBar.setValue(int(frame_id / per))
        QApplication.processEvents()
        
        ret, frame = capture.read()
        if not ret:
            break
        timer.tic()
        online_tlwhs, online_scores, online_ids = detector.predict(
            frame, FLAGS.threshold)
        timer.toc()

        results.append((frame_id + 1, online_tlwhs, online_scores, online_ids))
        fps = 1. / timer.average_time
        online_im = mot_vis.plot_tracking(
            frame,
            online_tlwhs,
            online_ids,
            online_scores,
            frame_id=frame_id,
            fps=fps)
        if FLAGS.save_images:
            save_dir = os.path.join(FLAGS.output_dir, video_name.split('.')[-2])
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            cv2.imwrite(
                os.path.join(save_dir, '{:05d}.jpg'.format(frame_id)),
                online_im)
        frame_id += 1
        print('detect frame:%d' % (frame_id))
        im = np.array(online_im)
        writer.write(im)
        
        # if camera_id == -1:
        #     cv2.imshow('Tracking Detection', im)
        #     if cv2.waitKey(1) == 27:
        #         break
        # if camera_id != -1:
        #     cv2.imshow('Tracking Detection', im)
        #     if cv2.waitKey(1) == 27:
        #         break

    if FLAGS.save_results:
        result_filename = os.path.join(FLAGS.output_dir,
                                       video_name.split('.')[-2] + '.txt')
        write_mot_results(result_filename, results)
    writer.release()

    #new
    progressBar.close()

    return video_name


def predict_camera(detector, camera_id):
    if camera_id != -1:
        capture = cv2.VideoCapture(camera_id)
        video_name = 'output-'+time.strftime("%m%d%H%M%S", time.localtime())+'.mp4'
    else:
        capture = cv2.VideoCapture(FLAGS.video_file)
        video_name = os.path.split(FLAGS.video_file)[-1]
    fps = 30
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))

    print(video_name)
    # print('frame_count', frame_count)

    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # yapf: disable 
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    # yapf: enable
    if not os.path.exists(FLAGS.output_dir):
        os.makedirs(FLAGS.output_dir)
    out_path = os.path.join(FLAGS.output_dir, video_name)
    writer = cv2.VideoWriter(out_path, fourcc, fps, (width, height))
    frame_id = 0
    timer = MOTTimer()
    results = []

    
    while (True):
        ret, frame = capture.read()
        if not ret:
            break
        timer.tic()
        online_tlwhs, online_scores, online_ids = detector.predict(
            frame, FLAGS.threshold)
        timer.toc()

        results.append((frame_id + 1, online_tlwhs, online_scores, online_ids))
        fps = 1. / timer.average_time
        online_im = mot_vis.plot_tracking(
            frame,
            online_tlwhs,
            online_ids,
            online_scores,
            frame_id=frame_id,
            fps=fps)
        if FLAGS.save_images:
            save_dir = os.path.join(FLAGS.output_dir, video_name.split('.')[-2])
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            cv2.imwrite(
                os.path.join(save_dir, '{:05d}.jpg'.format(frame_id)),
                online_im)
        frame_id += 1
        print('detect frame:%d' % (frame_id))
        im = np.array(online_im)
        writer.write(im)
        
        # if camera_id == -1:
        #     cv2.imshow('Tracking Detection', im)
        #     if cv2.waitKey(1) == 27:
        #         break
        if camera_id != -1:
            cv2.imshow('Tracking Detection-Esc exit', im)
            if cv2.waitKey(1) == 27:
                cv2.destroyAllWindows()
                break

    if FLAGS.save_results:
        result_filename = os.path.join(FLAGS.output_dir,
                                       video_name.split('.')[-2] + '.txt')
        write_mot_results(result_filename, results)
    writer.release()

# def main():
#     pred_config = PredictConfig_MOT(FLAGS.model_dir)
#     detector = MOT_Detector(
#         pred_config,
#         FLAGS.model_dir,
#         use_gpu=FLAGS.use_gpu,
#         run_mode=FLAGS.run_mode,
#         trt_min_shape=FLAGS.trt_min_shape,
#         trt_max_shape=FLAGS.trt_max_shape,
#         trt_opt_shape=FLAGS.trt_opt_shape,
#         trt_calib_mode=FLAGS.trt_calib_mode,
#         cpu_threads=FLAGS.cpu_threads,
#         enable_mkldnn=FLAGS.enable_mkldnn)

#     # predict from video file or camera video stream
#     if FLAGS.video_file is not None or FLAGS.camera_id != -1:
#         predict_video(detector, FLAGS.camera_id)
#     else:
#         print('MOT models do not support predict single image.')

# def infer_camera(camera_id):

def infer_video(model_dir, vidoe_file, threshold):
    paddle.enable_static()
    parser = argsparser()
    global FLAGS
    FLAGS = parser.parse_args()

    FLAGS.model_dir = model_dir
    FLAGS.video_file = vidoe_file
    FLAGS.use_gpu = True
    FLAGS.threshold = threshold

    pred_config = PredictConfig_MOT(FLAGS.model_dir)
    detector = MOT_Detector(
        pred_config,
        FLAGS.model_dir,
        use_gpu=FLAGS.use_gpu,
        run_mode=FLAGS.run_mode,
        trt_min_shape=FLAGS.trt_min_shape,
        trt_max_shape=FLAGS.trt_max_shape,
        trt_opt_shape=FLAGS.trt_opt_shape,
        trt_calib_mode=FLAGS.trt_calib_mode,
        cpu_threads=FLAGS.cpu_threads,
        enable_mkldnn=FLAGS.enable_mkldnn)
    global is_run
    is_run = True
    # predict from video file or camera video stream
    if FLAGS.video_file is not None or FLAGS.camera_id != -1:
        return predict_video(detector, FLAGS.camera_id)
    else:
        print('MOT models do not support predict single image.')



def infer_camera(model_dir, camera_id, threshold):
    paddle.enable_static()
    parser = argsparser()
    global FLAGS
    FLAGS = parser.parse_args()

    FLAGS.model_dir = model_dir
    FLAGS.camera_id = camera_id
    FLAGS.use_gpu = True
    FLAGS.threshold = threshold

    pred_config = PredictConfig_MOT(FLAGS.model_dir)
    detector = MOT_Detector(
        pred_config,
        FLAGS.model_dir,
        use_gpu=FLAGS.use_gpu,
        run_mode=FLAGS.run_mode,
        trt_min_shape=FLAGS.trt_min_shape,
        trt_max_shape=FLAGS.trt_max_shape,
        trt_opt_shape=FLAGS.trt_opt_shape,
        trt_calib_mode=FLAGS.trt_calib_mode,
        cpu_threads=FLAGS.cpu_threads,
        enable_mkldnn=FLAGS.enable_mkldnn)

    # predict from video file or camera video stream
    if FLAGS.video_file is not None or FLAGS.camera_id != -1:
        predict_camera(detector, FLAGS.camera_id)
    else:
        print('MOT models do not support predict single image.')


class MyProgressBar(QDialog):
    def __init__(self,parent = None):
        super(MyProgressBar, self).__init__(parent)
 
        self.resize(350,100)    #??????
        self.setWindowTitle(self.tr("Processing progress"))
 
        # self.TipLabel = QLabel(self.tr("Processing:" + "   " + str(fileIndex) + "/" + str(filenum)))
        self.FeatLabel = QLabel(self.tr("Extract feature:"))
        
        self.FeatProgressBar = QProgressBar(self)
        self.FeatProgressBar.setMinimum(0)
        self.FeatProgressBar.setMaximum(100) #??????????????????100
        self.FeatProgressBar.setValue(0) #?????????????????????0
 
        # TipLayout = QHBoxLayout()
        # TipLayout.addWidget(self.TipLabel)
 
        FeatLayout = QHBoxLayout()
        FeatLayout.addWidget(self.FeatLabel)
        FeatLayout.addWidget(self.FeatProgressBar)
 
        # self.startButton = QPushButton('start',self)
        self.cancelButton = QPushButton('cancel', self)
        # self.cancelButton.setFocusPolicy(Qt.NoFocus)
 
        buttonlayout = QHBoxLayout()
        buttonlayout.addStretch(1)
        buttonlayout.addWidget(self.cancelButton)
        # buttonlayout.addStretch(1)
        # buttonlayout.addWidget(self.startButton)
 
        layout = QVBoxLayout()
        # layout = QGridLayout()
        layout.addLayout(FeatLayout)
        # layout.addLayout(TipLayout)
        layout.addLayout(buttonlayout)
        self.setLayout(layout)
        self.show()
 
        # self.startButton.clicked.connect(self.setValue)
 
        self.cancelButton.clicked.connect(self.onCancel)    #??????
        # self.startButton.clicked.connect(self.onStart)
        # self.timer = QBasicTimer()
        # self.step = 0

    def setValue(self,value):
        self.FeatProgressBar.setValue(value) 
 
    def onCancel(self,event):
        global is_run
        is_run = False
        self.close()

    def closeEvent(self, event):
        # result = QtWidgets.QMessageBox.question(self, "Xpath Robot", "Do you want to exit?",
        #                                         QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        # if (result == QtWidgets.QMessageBox.Yes):
        #     event.accept()
        # else:
        #     event.ignore()
        global is_run
        is_run = False
        self.close()
        
        
# if __name__ == '__main__':
#     paddle.enable_static()
#     parser = argsparser()
#     FLAGS = parser.parse_args()
#     print_arguments(FLAGS)

#     main()
