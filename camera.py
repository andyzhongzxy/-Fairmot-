

from ui.ui_camera import Ui_Dialog
from PyQt5.QtWidgets import QDialog

import mot_infer

class MyCameraDialog(QDialog):
  def __init__(self):
    QDialog.__init__(self)
    self.child=Ui_Dialog()  #子窗口的实例化
    self.child.setupUi(self)
    # super(MyCameraDialog, self).__init__()
    # self.setupUi(self)
    self.child.horizontalSlider.setValue(30)
    self.child.horizontalSlider.valueChanged.connect(self.changeValue)  
    self.child.pushButton.clicked.connect(self.process_video)

  def changeValue(self):
    self.child.label_2.setNum(self.child.horizontalSlider.value() * 0.01)

  def process_video(self):
          
      threshold = float(self.child.label_2.text())

      mot_infer.infer_camera(model_dir="model", camera_id=0, threshold=threshold)


