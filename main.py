
import sys
from PyQt5.QtWidgets import QApplication, QFileDialog, QMainWindow
from ui.ui_main import *
from camera import MyCameraDialog
from video import MyVideoDialog
import mot_infer

class MyWindow(QMainWindow, Ui_MainWindow):
    def __init__(self):
        super(MyWindow, self).__init__()
        self.setupUi(self)

    def open_camera(self):
        # thread = Thread(target=self.btn_camera) 
        # thread.start()
        mot_infer.infer_camera(model_dir="model", camera_id=0)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    w = MyWindow()
    camera_ui = MyCameraDialog()
    video_ui = MyVideoDialog()
    w.pushButton.clicked.connect(camera_ui.show)
    w.pushButton_2.clicked.connect(video_ui.show)
    w.show()
    sys.exit(app.exec_()) 
