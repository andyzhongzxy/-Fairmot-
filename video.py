import sys

import cv2

from ui.ui_video import Ui_Dialog
from PyQt5.QtWidgets import  QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QVBoxLayout
import mot_infer

class MyVideoDialog(QDialog):
  def __init__(self):
    QDialog.__init__(self)
    self.child=Ui_Dialog()  #子窗口的实例化
    self.child.setupUi(self)

    #slider default value
    self.child.horizontalSlider.setValue(30)
    # self.child.label_4.setNum(self.child.horizontalSlider.value() * 0.01)

    self.child.horizontalSlider.valueChanged.connect(self.changeValue)  
    self.child.pushButton.clicked.connect(self.process_video)
    self.child.pushButton_2.clicked.connect(self.open_file)


  def changeValue(self):
    self.child.label_4.setNum(self.child.horizontalSlider.value() * 0.01)

  def open_file(self):
      file = QFileDialog.getOpenFileName(self,'Select File','','Video files(*.mp4)')
      self.child.lineEdit.setText(file[0])


  def process_video(self):
      file = self.child.lineEdit.text()
      
      if not file.strip():
        reply = QMessageBox.question(self, 'Warning', 'Input path does not exist！', QMessageBox.Yes)
        if reply == QMessageBox.Yes:
          return
          
      threshold = float(self.child.label_4.text())

      global video_name
      video_name = mot_infer.infer_video(model_dir="model", vidoe_file=file, threshold=threshold)

      #处理完 添加控件
      self.add()

  def display(self):
      na ="output/"+video_name
      capture = cv2.VideoCapture(na)
      while(True):
          ret, frame = capture.read()
          if(ret == True):
             cv2.imshow('Display', frame)
          else:
              cv2.destroyAllWindows()
          if cv2.waitKey(1) == 27:
              cv2.destroyAllWindows()
              break



  def add(self):
      self.lb1 = QPushButton("Display-Esc exit", self)
      self.lb1.setGeometry(190,230, 100, 23)
      # widget = QPushButton(self.lb1, self)

      self.lb1.clicked.connect(self.display)
      self.lb1.show()

