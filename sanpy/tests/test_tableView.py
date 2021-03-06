import os, sys, shutil, tempfile
import unittest

from PyQt5 import QtCore, QtGui, QtWidgets

import sanpy.interface

app = QtWidgets.QApplication(sys.argv)

class Test(unittest.TestCase):
	def setUp(self):
		"""Set up a temporary directory with some files
		"""
		# Create a temporary directory
		self.test_dir = tempfile.mkdtemp()

		self.myNumFilesToTest = 1
		# copy two files into it
		# 1
		dst = os.path.join(self.test_dir, '19114001.abf')
		shutil.copyfile('data/19114001.abf', dst)
		# 2
		# TODO: fix loading csv with new 2d _sweepX etc and pu tthis back in
		#dst = os.path.join(self.test_dir, '19114001.csv')
		#shutil.copyfile('data/19114001.csv', dst)

		self.ad = sanpy.analysisDir(self.test_dir)

	def tearDown(self):
		shutil.rmtree(self.test_dir)

	def test_1(self):
		model = sanpy.interface.bFileTable.pandasModel(self.ad)
		self.btv = sanpy.interface.bTableView(model)

		# This is becoming too abstract for me
		self.assertEqual(len(self.btv.model()._data), self.myNumFilesToTest)

if __name__ == "__main__":

	unittest.main()
	pass

	# I want to use this as both unit-test and direct command line to check my clicking???
	#btv.show()
	#sys.exit(app.exec_())
