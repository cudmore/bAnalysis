#Author: Robert H Cudmore
#Date: 20190225
"""
The bAnalysis class represents a whole-cell recording and provides functions for analysis.

A bAnalysis object can be created in a number of ways:
 (i) From a file path including .abf and .csv
 (ii) From a pandas DataFrame when loading from a h5 file.
 (iii) From a byteStream abf when working in the cloud.

Once loaded, a number of operations can be performed including:
  Spike detection, Error checking, Plotting, and Saving.

Examples:

```python
path = 'data/19114001.abf'
ba = bAnalysis(path)
dDict = sanpy.bAnalysis.getDefaultDetection()
ba.spikeDetect(dDict)
```
"""

import os, sys, math, time, collections, datetime
import uuid
from collections import OrderedDict
import warnings  # to catch np.polyfit -->> RankWarning: Polyfit may be poorly conditioned

import numpy as np
import pandas as pd
import scipy.signal

import pyabf  # see: https://github.com/swharden/pyABF

import sanpy
from sanpy.sanpyLogger import get_logger
logger = get_logger(__name__)


class bAnalysis:
	def getNewUuid():
		return 't' + str(uuid.uuid4()).replace('-', '_')

	def getDefaultDetection(cellType=None):
		"""
		Get default detection dictionary, pass this to [bAnalysis.spikeDetect()][sanpy.bAnalysis.bAnalysis.spikeDetect]

		Returns:
			dict: Dictionary of detection parameters.
		"""

		#cellType = 'neuron'

		mvThreshold = -20
		theDict = {
			'dvdtThreshold': 100, #if None then detect only using mvThreshold
			'mvThreshold': mvThreshold,
			'medianFilter': 0,
			'SavitzkyGolay_pnts': 5, # shoould correspond to about 0.5 ms
			'SavitzkyGolay_poly': 2,
			'halfHeights': [10, 20, 50, 80, 90],
			# new 20210501
			'mdp_ms': 250, # window before/after peak to look for MDP
			'refractory_ms': 170, # rreject spikes with instantaneous frequency
			'peakWindow_ms': 100, #10, # time after spike to look for AP peak
			'dvdtPreWindow_ms': 10, #5, # used in dvdt, pre-roll to then search for real threshold crossing
			'avgWindow_ms': 5,
			# 20210425, trying 0.15
			#'dvdt_percentOfMax': 0.1, # only used in dvdt detection, used to back up spike threshold to more meaningful value
			'dvdt_percentOfMax': 0.1, # only used in dvdt detection, used to back up spike threshold to more meaningful value
			# 20210413, was 50 for manuscript, we were missing lots of 1/2 widths
			'halfWidthWindow_ms': 200, #200, #20,
			# add 20210413 to turn of doBackupSpikeVm on pure vm detection
			'doBackupSpikeVm': True,
			'spikeClipWidth_ms': 500,
			'onlyPeaksAbove_mV': mvThreshold,
			'startSeconds': None, # not used ???
			'stopSeconds': None,

			# for detection of Ca from line scans
			#'caThresholdPos': 0.01,
			#'caMinSpike': 0.5,

			# todo: get rid of this
			# book keeping like ('cellType', 'sex', 'condition')
			'cellType': '',
			'sex': '',
			'condition': '',
			'verbose': False,
		}

		if cellType is not None:
			if cellType == 'SA Node Params':
				# these are defaults from above
				pass
			elif cellType == 'Ventricular Params':
				theDict['dvdtThreshold'] = 100
				theDict['mvThreshold'] = -20
				theDict['refractory_ms'] = 200  # max freq of 5 Hz
				theDict['peakWindow_ms'] = 100
				theDict['halfWidthWindow_ms'] = 300
				theDict['spikeClipWidth_ms'] = 200
			elif cellType == 'Neuron Params':
				theDict['dvdtThreshold'] = 100
				theDict['mvThreshold'] = -20
				theDict['refractory_ms'] = 7
				theDict['peakWindow_ms'] = 5
				theDict['halfWidthWindow_ms'] = 4
				theDict['spikeClipWidth_ms'] = 20
			else:
				logger.error(f'Did not understand cell type "{cellType}"')

		return theDict.copy()

	def __init__(self, file=None, theTiff=None, byteStream=None, fromDf=None):
		"""
		Args:
			file (str): Path to either .abf or .csv with time/mV columns.
			theTiff (str): Path to .tif file.
			byteStream (binary): Binary stream for use in the cloud.
			fromDf: (pd.DataFrame) one row df with columns as instance variables
		"""
		#logger.info(f'{file}')

		# mimic pyAbf
		self._dataPointsPerMs = None
		self._sweepList = [0]  # list
		#self._currentSweep = 0  # int
		self._sweepX = None  # np.ndarray
		self._sweepY = None  # np.ndarray
		self._sweepC = None # the command waveform (DAC)

		self._recordingMode = None  # str
		self._sweepLabelX = '???'  # str
		self._sweepLabelY = '???'  # str

		self.myFileType = None
		"""str: From ('abf', 'csv', 'tif', 'bytestream')"""

		self.loadError = False
		"""bool: True if error loading file/stream."""

		self.detectionDict = None  # remember the parameters of our last detection
		"""dict: Dictionary specifying detection parameters, see getDefaultDetection."""

		self._path = file  # todo: change this to filePath
		"""str: File path."""

		self._abf = None
		"""pyAbf: If loaded from binary .abf file"""

		self.dateAnalyzed = None
		"""str: Date Time of analysis. TODO: make a property."""

		self.detectionType = None
		"""str: From ('dvdt', 'mv')"""

		self._filteredVm = None
		self._filteredDeriv = None

		self.spikeDict = []  # a list of dict
		#self.spikeTimes = []  # created in self.spikeDetect()

		self.spikeClips = []  # created in self.spikeDetect()
		self.spikeClips_x = []  #
		self.spikeClips_x2 = []  #

		self.dfError = None  # dataframe with a list of detection errors
		self.dfReportForScatter = None  # dataframe to be used by scatterplotwidget

		if file is not None and not os.path.isfile(file):
			logger.error(f'File does not exist: "{file}"')
			self.loadError = True

		# only defined when loading abf files
		self.acqDate = None
		self.acqTime = None

		#self._currentSweep = None
		#self.setSweep(0)

		self._detectionDirty = False

		# will be overwritten by existing uuid in self._loadFromDf()
		self.uuid = bAnalysis.getNewUuid()

		# IMPORTANT:
		#		All instance variable MUST be declared before we load
		#		In particular for self._loadFromDf()

		# instantiate and load abf file
		if fromDf is not None:
			self._loadFromDf(fromDf)
		elif byteStream is not None:
			self._loadAbf(byteStream=byteStream)
		elif file.endswith('.abf'):
			self._loadAbf()
		elif file.endswith('.tif'):
			self._loadTif()
		elif file.endswith('.csv'):
			self._loadCsv()
		else:
			logger.error(f'Can only open abf/csv/tif/stream files: {file}')
			self.loadError = True

		# get default derivative
		self.rebuildFiltered()
		'''
		if self._recordingMode == 'I-Clamp':
			self._getDerivative()
		elif self._recordingMode == 'V-Clamp':
			self._getBaselineSubtract()
		else:
			logger.warning('Did not take derivative')
		'''

		self._detectionDirty = False

	def __str__(self):
		return f'ba: {self.getFileName()} dur:{round(self.recordingDur,3)} spikes:{self.numSpikes} isAnalyzed:{self.isAnalyzed()} detectionDirty:{self.detectionDirty}'

	def _loadTif(self):
		#print('TODO: load tif file from within bAnalysis ... stop using bAbfText()')
		self._abf = sanpy.bAbfText(file)
		self._abf.sweepY = self._normalizeData(self._abf.sweepY)
		self.myFileType = 'tif'

	def _loadCsv(self):
		"""
		Load from a two column CSV file with columns of (s, mV)
		"""
		logger.info(self._path)

		dfCsv = pd.read_csv(self._path)

		# TODO: check column names make sense
		# There must be 2 columns ('s', 'mV')
		numCols = len(dfCsv.columns)
		if numCols != 2:
			# error
			logger.warning(f'There must be two columns, found {numCols}')
			self.loadError = True
			return

		self.myFileType = 'csv'
		self._sweepX = dfCsv['s'].values  # first col is time
		self._sweepY = dfCsv['mV'].values  # second col is values (either mV or pA)

		# TODO: infer from second column
		self._recordingMode = 'I-Clamp'

		# TODO: infer from columns
		self._sweepLabelX = 's' # TODO: get from column
		self._sweepLabelY = 'mV' # TODO: get from column

		# TODO: infer from first column as ('s', 'ms')
		firstPnt = self._sweepX[0]
		secondPnt = self._sweepX[1]
		diff_seconds = secondPnt - firstPnt
		diff_ms = diff_seconds * 1000
		_dataPointsPerMs = 1 / diff_ms
		self._dataPointsPerMs = _dataPointsPerMs
		logger.info(f'_dataPointsPerMs: {_dataPointsPerMs}')

	def _loadFromDf(self, fromDf):
		"""Load from a pandas df saved into a .h5 file.
		"""
		logger.info(fromDf['uuid'][0])

		# vars(class) retuns a dict with all instance variables
		iDict = vars(self)
		for col in fromDf.columns:
			value = fromDf.iloc[0][col]
			#logger.info(f'col:{col} {type(value)}')
			if col not in iDict.keys():
				logger.warning(f'col "{col}" not in iDict')
			iDict[col] = value

		#logger.info(f'sweepX:{self.sweepX.shape}')
		#logger.info(f'sweepList:{self.sweepList}')
		#logger.info(f'_currentSweep:{self._currentSweep}')

	def _saveToHdf(self, hdfPath):
		"""Save to h5 file with key self.uuid.
		Only save if detection has changed (e.g. self.detectionDirty)
		"""
		didSave = False
		if not self.detectionDirty:
			# Do not save it detection has not changed
			logger.info(f'NOT SAVING uuid:{self.uuid} {self}')
			return didSave

		logger.info(f'SAVING uuid:{self.uuid} {self}')

		with pd.HDFStore(hdfPath, mode='a') as hdfStore:
			# vars(class) retuns a dict with all instance variables
			iDict = vars(self)

			oneDf = pd.DataFrame(columns=iDict.keys())
			#oneDf['path'] = [self.path]  # seed oneDf with one row (critical)

			# do not save these instance variables (e.g. self._ba)
			noneKeys = ['_abf', '_filteredVm', '_filteredDeriv',
						'spikeClips', 'spikeClips_x', 'spikeClips_x2']

			for k,v in iDict.items():
				if k in noneKeys:
					v = None
				#print(f'saving h5 k:{k} {type(v)}')
				oneDf.at[0, k] = v
			# save into file
			hdfStore[self.uuid] = oneDf

			#
			self._detectionDirty = False
			didSave= True
		#
		return didSave

	def _loadAbf(self, byteStream=None):
		"""Load pyAbf from path."""
		try:
			if byteStream is not None:
				self._abf = pyabf.ABF(byteStream)
			else:
				self._abf = pyabf.ABF(self._path)

			self._sweepList = self._abf.sweepList

			# on load, sweep is 0
			tmpRows = self._abf.sweepX.shape[0]
			numSweeps = len(self._sweepList)
			self._sweepX = np.zeros((tmpRows,numSweeps))
			self._sweepY = np.zeros((tmpRows,numSweeps))
			self._sweepC = np.zeros((tmpRows,numSweeps))
			'''
			print('=== _loadAbf')
			print(f'self._sweepX is {self._sweepX.shape}')
			print(f'self._sweepY is {self._sweepY.shape}')
			print(f'self._sweepC is {self._sweepC.shape}')
			'''
			for sweep in self._sweepList:
				self._abf.setSweep(sweep)
				self._sweepX[:, sweep] = self._abf.sweepX  # <class 'numpy.ndarray'>, (60000,)
				self._sweepY[:, sweep] = self._abf.sweepY
				self._sweepC[:, sweep] = self._abf.sweepC
			#print(f'self._sweepX is {self._sweepX.shape}')
			self._abf.setSweep(0)

			# get v from pyAbf
			self._dataPointsPerMs = self._abf.dataPointsPerMs

			abfDateTime = self._abf.abfDateTime  # 2019-01-14 15:20:48.196000
			self.acqDate = abfDateTime.strftime("%Y-%m-%d")
			self.acqTime = abfDateTime.strftime("%H:%M:%S")

			# TODO: fix this
			#self._sweepX_label = 's'
			self._sweepLabelX = self._abf.sweepLabelX
			self._sweepLabelY = self._abf.sweepLabelY

			if self._abf.sweepUnitsY in ['pA']:
				self._recordingMode = 'V-Clamp'
				self._sweepY_label = self._abf.sweepUnitsY
			elif self._abf.sweepUnitsY in ['mV']:
				self._recordingMode = 'I-Clamp'
				self._sweepY_label = self._abf.sweepUnitsY

		except (NotImplementedError) as e:
			logger.error(f'did not load abf file: {self._path}')
			logger.error(f'  NotImplementedError exception was: {e}')
			self.loadError = True
			self._abf = None

		except (Exception) as e:
			# some abf files throw: 'unpack requires a buffer of 234 bytes'
			logger.error(f'did not load abf file: {self._path}')
			logger.error(f'  unknown Exception was: {e}')
			self.loadError = True
			self._abf = None

		#
		self.myFileType = 'abf'

	@property
	def detectionDirty(self):
		return self._detectionDirty

	@property
	def path(self):
		return self._path

	def getFileName(self):
		if self._path is None:
			return None
		else:
			return os.path.split(self._path)[1]

	@property
	def abf(self):
		"""Get the underlying pyabf object."""
		return self._abf

	@property
	def recordingDur(self):
		"""Get recording duration in seconds."""
		# TODO: Just return self.sweepX(-1)
		#return len(self.sweepX) / self.dataPointsPerMs / 1000
		theDur = self._sweepX[-1,0] # last point in first sweep ???
		#logger.info(f'theDur:{theDur} {type(theDur)}')
		return theDur

	@property
	def recordingFrequency(self):
		"""Get recording frequency in kHz."""
		return self._dataPointsPerMs

	'''
	@property
	def currentSweep(self):
		return self._currentSweep
	'''

	@property
	def dataPointsPerMs(self):
		"""Get the number of data points per ms."""
		return self._dataPointsPerMs

	'''
	def setSweep(self, sweepNumber):
		"""
		Set the current sweep.

		Args:
			sweepNumber (str): from ('All', 0, 1, 2, 3, ...)

		TODO:
			- Set sweep of underlying abf (if we have one)
			- take channel into account with:
				abf.setSweep(sweepNumber: 3, channel: 0)
		"""
		if sweepNumber == 'All':
			sweepNumber = 'allsweeps'
		elif sweepNumber == 'allsweeps':
			pass
		else:
			sweepNumber = int(sweepNumber)
		self._currentSweep = sweepNumber
		self.rebuildFiltered()
	'''

	@property
	def sweepList(self):
		"""Get the list of sweeps."""
		return self._sweepList

	@property
	def numSweeps(self):
		"""Get the number of sweeps."""
		return len(self._sweepList)

	@property
	def numSpikes(self):
		"""Get the total number of detected spikes."""
		#return len(self.spikeTimes) # spikeTimes is tmp per sweep
		return len(self.spikeDict) # spikeDict has all spikes for all sweeps

	#@property
	def sweepX(self, sweepNumber=None):
		"""Get the time (seconds) from recording (numpy.ndarray)."""
		if sweepNumber is None or sweepNumber == 'All':
			return self._sweepX
		else:
			return self._sweepX[:,sweepNumber]

	#@property
	def sweepY(self, sweepNumber=None):
		"""Get the amplitude (mV or pA) from recording (numpy.ndarray). Units wil depend on mode"""
		if sweepNumber is None or sweepNumber == 'All':
			return self._sweepY
		else:
			return self._sweepY[:,sweepNumber]

	#@property
	def sweepC(self, sweepNumber=None):
		"""Get the command waveform DAC (numpy.ndarray). Units will depend on mode"""
		if sweepNumber is None or sweepNumber == 'All':
			return self._sweepC
		else:
			return self._sweepC[:,sweepNumber]

	#@property
	def filteredDeriv(self, sweepNumber=None):
		"""Get the command waveform DAC (numpy.ndarray). Units will depend on mode"""
		#logger.info(self._filteredDeriv.shape)
		if sweepNumber is None or sweepNumber == 'All':
			return self._filteredDeriv
		else:
			return self._filteredDeriv[:,sweepNumber]

	#@property
	def filteredVm(self, sweepNumber=None):
		"""Get the command waveform DAC (numpy.ndarray). Units will depend on mode"""
		if sweepNumber is None or sweepNumber == 'All':
			return self._filteredVm
		else:
			return self._filteredVm[:,sweepNumber]

	def get_yUnits(self):
		return self._sweepLabelY

	def get_xUnits(self):
		return self._sweepLabelX

	def isAnalyzed(self):
		"""Return True if this bAnalysis has been analyzed, False otherwise."""
		return self.detectionDict is not None
		#return self.numSpikes > 0

	def getStatMean(self, statName, sweepNumber=None):
		"""
		Get the mean of an analysis parameter.

		Args:
			statName (str): Name of the statistic to retreive.
				For a list of available stats use bDetection.defaultDetection.
		"""
		theMean = None
		x = self.getStat(statName, sweepNumber=sweepNumber)
		if x is not None and len(x)>1:
			theMean = np.nanmean(x)
		return theMean

	def getStat(self, statName1, statName2=None, sweepNumber=None):
		"""
		Get a list of values for one or two analysis parameters.

		For a list of available analysis parameters, use [bDetection.getDefaultDetection()][sanpy.bDetection.bDetection]

		If the returned list of analysis parameters are in points,
			convert to seconds or ms using: pnt2Sec_(pnt) or pnt2Ms_(pnt).

		Args:
			statName1 (str): Name of the first analysis parameter to retreive.
			statName2 (str): Optional, Name of the second analysis parameter to retreive.

		Returns:
			list: List of analysis parameter values, None if error.

		TODO: Add convertToSec (bool)
		"""
		def clean(val):
			"""Convert None to float('nan')"""
			if val is None:
				val = float('nan')
			return val

		x = []  # None
		y = []  # None
		error = False

		if len(self.spikeDict) == 0:
			#logger.warning(f'Did not find any spikes in spikeDict')
			error = True
		elif statName1 not in self.spikeDict[0].keys():
			logger.warning(f'Did not find statName1: "{statName1}" in spikeDict')
			error = True
		elif statName2 is not None and statName2 not in self.spikeDict[0].keys():
			logger.warning(f'Did not find statName2: "{statName2}" in spikeDict')
			error = True

		if sweepNumber is None:
			sweepNumber=='All'

		if not error:
			# original
			#x = [clean(spike[statName1]) for spike in self.spikeDict]
			# only current spweek
			x = [clean(spike[statName1]) for spike in self.spikeDict if sweepNumber=='All' or spike['sweep']==sweepNumber]

			if statName2 is not None:
				# original
				#y = [clean(spike[statName2]) for spike in self.spikeDict]
				# only current spweek
				y = [clean(spike[statName2]) for spike in self.spikeDict if sweepNumber=='All' or spike['sweep']==sweepNumber]

		if statName2 is not None:
			return x, y
		else:
			return x

	def getSpikeTimes(self, sweepNumber=None):
		"""Get spike times for current sweep
		"""
		#theRet = [spike['thresholdPnt'] for spike in self.spikeDict if spike['sweep']==self.currentSweep]
		theRet = self.getStat('thresholdPnt', sweepNumber=sweepNumber)
		return theRet

	def getSpikeSeconds(self, sweepNumber=None):
		#theRet = [spike['thresholdSec'] for spike in self.spikeDict if spike['sweep']==self.currentSweep]
		theRet = self.getStat('thresholdSec')
		return theRet

	def getSpikeDictionaries(self, sweepNumber=None):
		"""Get spike dictionaries for current sweep
		"""
		if sweepNumber is None:
			sweepNumber = 'All'
		#currentSweep = self.currentSweep
		theRet = [spike for spike in self.spikeDict if sweepNumber=='All' or spike['sweep']==sweepNumber]
		return theRet

	def rebuildFiltered(self):
		if self._recordingMode == 'I-Clamp':
			self._getDerivative()
		elif self._recordingMode == 'V-Clamp':
			self._getBaselineSubtract()
		else:
			logger.warning('Did not take derivative')

	def _getFilteredRecording(self, dDict=None):
		"""
		Get a filtered version of recording, used for both V-Clamp and I-Clamp.

		Args:
			dDict (dict): Default detection dictionary. See bDetection.defaultDetection
		"""
		if dDict is None:
			dDict = bAnalysis.getDefaultDetection()

		medianFilter = dDict['medianFilter']
		SavitzkyGolay_pnts = dDict['SavitzkyGolay_pnts']
		SavitzkyGolay_poly = dDict['SavitzkyGolay_poly']

		if medianFilter > 0:
			if not medianFilter % 2:
				medianFilter += 1
				logger.warning(f'Please use an odd value for the median filter, set medianFilter: {medianFilter}')
			medianFilter = int(medianFilter)
			self._filteredVm = scipy.signal.medfilt2d(self.sweepY, [medianFilter,1])
		elif SavitzkyGolay_pnts > 0:
			self._filteredVm = scipy.signal.savgol_filter(self.sweepY,
								SavitzkyGolay_pnts, SavitzkyGolay_poly,
								mode='nearest', axis=0)
		else:
			self._filteredVm = self.sweepY

	def _getBaselineSubtract(self, dDict=None):
		"""
		for V-Clamp

		Args:
			dDict (dict): Default detection dictionary. See getDefaultDetection()
		"""
		print('\n\n THIS IS BROKEN BECAUSE OF SWEEPS IN FILTERED DERIV\n\n')

		if dDict is None:
			dDict = bAnalysis.getDefaultDetection()

		dDict['medianFilter'] = 5

		self._getFilteredRecording(dDict)

		# baseline subtract filtered recording
		theMean = np.nanmean(self.filteredVm)
		self.filteredDeriv = self.filteredVm.copy()
		self.filteredDeriv -= theMean

	def _getDerivative(self, dDict=None):
		"""
		Get derivative of recording (used for I-Clamp). Uses (xxx,yyy,zzz) keys in dDict.

		Args:
			dDict (dict): Default detection dictionary. See getDefaultDetection()
		"""
		if dDict is None:
			dDict = bAnalysis.getDefaultDetection()

		medianFilter = dDict['medianFilter']
		SavitzkyGolay_pnts = dDict['SavitzkyGolay_pnts']
		SavitzkyGolay_poly = dDict['SavitzkyGolay_poly']

		if medianFilter > 0:
			if not medianFilter % 2:
				medianFilter += 1
				logger.warning('Please use an odd value for the median filter, set medianFilter: {medianFilter}')
			medianFilter = int(medianFilter)
			self._filteredVm = scipy.signal.medfilt2d(self._sweepY, [medianFilter,1])
		elif SavitzkyGolay_pnts > 0:
			self._filteredVm = scipy.signal.savgol_filter(self._sweepY,
								SavitzkyGolay_pnts, SavitzkyGolay_poly,
								axis=0,
								mode='nearest')
		else:
			self._filteredVm = self._sweepY

		self._filteredDeriv = np.diff(self._filteredVm, axis=0)

		# filter the derivative
		if medianFilter > 0:
			if not medianFilter % 2:
				medianFilter += 1
				print(f'Please use an odd value for the median filter, set medianFilter: {medianFilter}')
			medianFilter = int(medianFilter)
			self._filteredDeriv = scipy.signal.medfilt2d(self._filteredDeriv, [medianFilter,1])
		elif SavitzkyGolay_pnts > 0:
			self._filteredDeriv = scipy.signal.savgol_filter(self._filteredDeriv,
									SavitzkyGolay_pnts, SavitzkyGolay_poly,
									axis = 0,
									mode='nearest')
		else:
			#self._filteredDeriv = self.filteredDeriv
			pass

		# mV/ms
		dataPointsPerMs = self.dataPointsPerMs
		self._filteredDeriv = self._filteredDeriv * dataPointsPerMs #/ 1000

		# insert an initial point (rw) so it is the same length as raw data in abf.sweepY
		# three options (concatenate, insert, vstack)
		# could only get vstack working
		#self.deriv = np.concatenate(([0],self.deriv))
		rowOfZeros = np.zeros(self.numSweeps)
		rowZero = 0
		self._filteredDeriv = np.vstack([rowOfZeros, self._filteredDeriv])
		#self._filteredDeriv = np.insert(self.filteredDeriv, rowZero, rowOfZeros, axis=0)
		#self._filteredDeriv = np.concatenate((zeroRow,self.filteredDeriv))
		#print('  self._filteredDeriv:', self._filteredDeriv[0:4,:])
	def getDefaultDetection_ca(self):
		"""
		Get default detection for Ca analysis. Warning, this is currently experimental.

		Returns:
			dict: Dictionary of detection parameters.
		"""
		theDict = bAnalysis.getDefaultDetection()
		theDict['dvdtThreshold'] = 0.01 #if None then detect only using mvThreshold
		theDict['mvThreshold'] = 0.5
		#
		#theDict['medianFilter': 0
		#'halfHeights': [20,50,80]
		theDict['refractory_ms'] = 200 #170 # reject spikes with instantaneous frequency
		#theDict['peakWindow_ms': 100 #10, # time after spike to look for AP peak
		#theDict['dvdtPreWindow_ms': 2 # used in dvdt, pre-roll to then search for real threshold crossing
		#theDict['avgWindow_ms': 5
		#theDict['dvdt_percentOfMax': 0.1
		theDict['halfWidthWindow_ms'] = 200 #was 20
		#theDict['spikeClipWidth_ms': 500
		#theDict['onlyPeaksAbove_mV': None
		#theDict['startSeconds': None
		#theDict['stopSeconds': None

		# for detection of Ca from line scans
		#theDict['caThresholdPos'] = 0.01
		#theDict['caMinSpike'] = 0.5

		return theDict.copy()

	def _backupSpikeVm(self, spikeTimes, sweepNumber, medianFilter=None):
		"""
		when detecting with just mV threshold (not dv/dt)
		backup spike time using deminishing SD and diff b/w vm at pnt[i]-pnt[i-1]

		Args:
			spikeTimes (list of float):
			medianFilter (int): bin width
		"""
		#realSpikeTimePnts = [np.nan] * self.numSpikes
		realSpikeTimePnts = [np.nan] * len(spikeTimes)

		medianFilter = 5
		sweepY = self.sweepY(sweepNumber)
		if medianFilter>0:
			myVm = scipy.signal.medfilt(sweepY, medianFilter)
		else:
			myVm = sweepY

		#
		# TODO: this is going to fail if spike is at start/stop of recorrding
		#

		maxNumPntsToBackup = 20 # todo: add _ms
		bin_ms = 1
		bin_pnts = bin_ms * self.dataPointsPerMs
		half_bin_pnts = math.floor(bin_pnts/2)
		for idx, spikeTimePnts in enumerate(spikeTimes):
			foundRealThresh = False
			thisMean = None
			thisSD = None
			backupNumPnts = 0
			atBinPnt = spikeTimePnts
			while not foundRealThresh:
				thisWin = myVm[atBinPnt-half_bin_pnts: atBinPnt+half_bin_pnts]
				if thisMean is None:
					thisMean = np.mean(thisWin)
					thisSD = np.std(thisWin)

				nextStart = atBinPnt-1-bin_pnts-half_bin_pnts
				nextStop = atBinPnt-1-bin_pnts+half_bin_pnts
				nextWin = myVm[nextStart:nextStop]
				nextMean = np.mean(nextWin)
				nextSD = np.std(nextWin)

				meanDiff = thisMean - nextMean
				# logic
				sdMult = 0.7 # 2
				if (meanDiff < nextSD * sdMult) or (backupNumPnts==maxNumPntsToBackup):
					# second clause will force us to terminate (this recording has a very slow rise time)
					# bingo!
					foundRealThresh = True
					# not this xxx but the previous
					moveForwardPnts = 4
					backupNumPnts = backupNumPnts - 1 # the prev is thresh
					if backupNumPnts<moveForwardPnts:
						logger.warning(f'spike {idx} backupNumPnts:{backupNumPnts} < moveForwardPnts:{moveForwardPnts}')
						#print('  -->> not adjusting spike time')
						realBackupPnts = backupNumPnts - 0
						realPnt = spikeTimePnts - (realBackupPnts*bin_pnts)

					else:
						realBackupPnts = backupNumPnts - moveForwardPnts
						realPnt = spikeTimePnts - (realBackupPnts*bin_pnts)
					#
					realSpikeTimePnts[idx] = realPnt

				# increment
				thisMean = nextMean
				thisSD = nextSD

				atBinPnt -= bin_pnts
				backupNumPnts += 1
				'''
				if backupNumPnts>maxNumPntsToBackup:
					print(f'  WARNING: _backupSpikeVm() exiting spike {idx} ... reached maxNumPntsToBackup:{maxNumPntsToBackup}')
					print('  -->> not adjusting spike time')
					foundRealThresh = True # set this so we exit the loop
					realSpikeTimePnts[idx] = spikeTimePnts
				'''

		#
		return realSpikeTimePnts

	def _throwOutRefractory(self, spikeTimes0, goodSpikeErrors, refractory_ms=20):
		"""
		spikeTimes0: spike times to consider
		goodSpikeErrors: list of errors per spike, can be None
		refractory_ms:
		"""
		before = len(spikeTimes0)

		# if there are doubles, throw-out the second one
		#refractory_ms = 20 #10 # remove spike [i] if it occurs within refractory_ms of spike [i-1]
		lastGood = 0 # first spike [0] will always be good, there is no spike [i-1]
		for i in range(len(spikeTimes0)):
			if i==0:
				# first spike is always good
				continue
			dPoints = spikeTimes0[i] - spikeTimes0[lastGood]
			if dPoints < self.dataPointsPerMs*refractory_ms:
				# remove spike time [i]
				spikeTimes0[i] = 0
			else:
				# spike time [i] was good
				lastGood = i
		# regenerate spikeTimes0 by throwing out any spike time that does not pass 'if spikeTime'
		# spikeTimes[i] that were set to 0 above (they were too close to the previous spike)
		# will not pass 'if spikeTime', as 'if 0' evaluates to False
		if goodSpikeErrors is not None:
			goodSpikeErrors = [goodSpikeErrors[idx] for idx, spikeTime in enumerate(spikeTimes0) if spikeTime]
		spikeTimes0 = [spikeTime for spikeTime in spikeTimes0 if spikeTime]

		# TODO: put back in and log if detection ['verbose']
		after = len(spikeTimes0)
		if self.detectionDict['verbose']:
			logger.info(f'From {before} to {after} spikes with refractory_ms:{refractory_ms}')

		return spikeTimes0, goodSpikeErrors

	def _getErrorDict(self, spikeNumber, pnt, type, detailStr):
		"""
		Get error dict for one spike
		"""
		sec = pnt / self.dataPointsPerMs / 1000
		sec = round(sec,4)

		eDict = {} #OrderedDict()
		eDict['Spike'] = spikeNumber
		eDict['Seconds'] = sec
		eDict['Type'] = type
		eDict['Details'] = detailStr
		return eDict

	def _spikeDetect_dvdt(self, dDict, sweepNumber, verbose=False):
		"""
		Search for threshold crossings (dvdtThreshold) in first derivative (dV/dt) of membrane potential (Vm)
		append each threshold crossing (e.g. a spike) in self.spikeTimes list

		Returns:
			self.spikeTimes (pnts): the time before each threshold crossing when dv/dt crosses 15% of its max
			self.filteredVm:
			self.filtereddVdt:
		"""

		#
		# header
		now = datetime.datetime.now()
		dateStr = now.strftime('%Y-%m-%d %H:%M:%S')
		self.dateAnalyzed = dateStr
		self.detectionType = 'dVdtThreshold'

		#
		# analyze full recording
		#startPnt = 0
		#stopPnt = len(self.sweepX) - 1
		#secondsOffset = 0

		filteredDeriv = self.filteredDeriv(sweepNumber)
		Is=np.where(filteredDeriv>dDict['dvdtThreshold'])[0]
		Is=np.concatenate(([0],Is))
		Ds=Is[:-1]-Is[1:]+1
		spikeTimes0 = Is[np.where(Ds)[0]+1]

		#
		# reduce spike times based on start/stop
		# only include spike times between startPnt and stopPnt
		#spikeTimes0 = [spikeTime for spikeTime in spikeTimes0 if (spikeTime>=startPnt and spikeTime<=stopPnt)]

		#
		# throw out all spikes that are below a threshold Vm (usually below -20 mV)
		peakWindow_pnts = self.dataPointsPerMs * dDict['peakWindow_ms']
		peakWindow_pnts = round(peakWindow_pnts)
		goodSpikeTimes = []
		sweepY = self.sweepY(sweepNumber=sweepNumber)
		for spikeTime in spikeTimes0:
			#peakVal = np.max(self.sweepY[spikeTime:spikeTime+peakWindow_pnts])
			peakVal = np.max(sweepY[spikeTime:spikeTime+peakWindow_pnts])
			if peakVal > dDict['mvThreshold']:
				goodSpikeTimes.append(spikeTime)
		spikeTimes0 = goodSpikeTimes

		#
		# throw out spike that are not upward deflections of Vm
		'''
		prePntUp = 7 # pnts
		goodSpikeTimes = []
		for spikeTime in spikeTimes0:
			preAvg = np.average(self.abf.sweepY[spikeTime-prePntUp:spikeTime-1])
			postAvg = np.average(self.abf.sweepY[spikeTime+1:spikeTime+prePntUp])
			#print(preAvg, postAvg)
			if preAvg < postAvg:
				goodSpikeTimes.append(spikeTime)
		spikeTimes0 = goodSpikeTimes
		'''

		#
		# if there are doubles, throw-out the second one
		spikeTimeErrors = None
		spikeTimes0, ignoreSpikeErrors = self._throwOutRefractory(spikeTimes0, spikeTimeErrors, refractory_ms=dDict['refractory_ms'])

		#
		# for each threshold crossing, search backwards in dV/dt for a % of maximum (about 10 ms)
		#dvdt_percentOfMax = 0.1
		#window_ms = 2
		window_pnts = dDict['dvdtPreWindow_ms'] * self.dataPointsPerMs
		# abb 20210130 lcr analysis
		window_pnts = round(window_pnts)
		spikeTimes1 = []
		spikeErrorList1 = []
		filteredDeriv = self.filteredDeriv(sweepNumber)  # sweepNumber is not optional
		for i, spikeTime in enumerate(spikeTimes0):
			# get max in derivative

			# 20210130, this is a BUG !!!! I was only looking before, should be looking before AND after
			preDerivClip = filteredDeriv[spikeTime-window_pnts:spikeTime] # backwards
			postDerivClip = filteredDeriv[spikeTime:spikeTime+window_pnts] # forwards

			# 20210130 lcr analysis now this
			#preDerivClip = self.filteredDeriv[spikeTime-window_pnts:spikeTime+window_pnts] # backwards

			if len(preDerivClip) == 0:
				print('error: spikeDetect_dvdt()',
						'spike', i, 'at pnt', spikeTime,
						'window_pnts:', window_pnts,
						'dvdtPreWindow_ms:', dDict['dvdtPreWindow_ms'],
						'len(preDerivClip)', len(preDerivClip))#preDerivClip = np.flip(preDerivClip)

			# look for % of max in dvdt
			try:
				#peakPnt = np.argmax(preDerivClip)
				peakPnt = np.argmax(postDerivClip)
				#peakPnt += spikeTime-window_pnts
				peakPnt += spikeTime
				peakVal = filteredDeriv[peakPnt]

				percentMaxVal = peakVal * dDict['dvdt_percentOfMax'] # value we are looking for in dv/dt
				preDerivClip = np.flip(preDerivClip) # backwards
				tmpWhere = np.where(preDerivClip<percentMaxVal)
				#print('tmpWhere:', type(tmpWhere), tmpWhere)
				tmpWhere = tmpWhere[0]
				if len(tmpWhere) > 0:
					threshPnt2 = np.where(preDerivClip<percentMaxVal)[0][0]
					threshPnt2 = (spikeTime) - threshPnt2
					#print('i:', i, 'spikeTime:', spikeTime, 'peakPnt:', peakPnt, 'threshPnt2:', threshPnt2)
					threshPnt2 -= 1 # backup by 1 pnt
					spikeTimes1.append(threshPnt2)
					spikeErrorList1.append(None)

				else:
					errType = 'dvdtPercent'
					errStr = f"dvdtPercent error searching for dvdt_percentOfMax: {dDict['dvdt_percentOfMax']} peak dV/dt is {peakVal}"
					spikeTimes1.append(spikeTime)
					eDict = self._getErrorDict(i, spikeTime, errType, errStr) # spikeTime is in pnts
					spikeErrorList1.append(eDict)
			except (IndexError, ValueError) as e:
				##
				print('   error: bAnalysis.spikeDetect_dvdt() looking for dvdt_percentOfMax')
				print('	  ', 'IndexError for spike', i, spikeTime)
				print('	  ', e)
				##
				spikeTimes1.append(spikeTime)

		return spikeTimes1, spikeErrorList1

	def _spikeDetect_vm(self, dDict, sweepNumber, verbose=False):
		"""
		spike detect using Vm threshold and NOT dvdt
		append each threshold crossing (e.g. a spike) in self.spikeTimes list

		Returns:
			self.spikeTimes (pnts): the time before each threshold crossing when dv/dt crosses 15% of its max
			self.filteredVm:
			self.filtereddVdt:
		"""

		#
		# header
		now = datetime.datetime.now()
		dateStr = now.strftime('%Y-%m-%d %H:%M:%S')
		self.dateAnalyzed = dateStr
		self.detectionType = 'mvThreshold'

		#
		#startPnt = 0
		#stopPnt = len(self.sweepX) - 1
		#secondsOffset = 0
		'''
		if dDict['startSeconds'] is not None and dDict['stopSeconds'] is not None:
			startPnt = self.dataPointsPerMs * (dDict['startSeconds']*1000) # seconds to pnt
			stopPnt = self.dataPointsPerMs * (dDict['stopSeconds']*1000) # seconds to pnt
		'''

		filteredVm = self.filteredVm(sweepNumber=sweepNumber)  # sweep number is not optional here
		Is=np.where(filteredVm>dDict['mvThreshold'])[0] # returns boolean array
		Is=np.concatenate(([0],Is))
		Ds=Is[:-1]-Is[1:]+1
		spikeTimes0 = Is[np.where(Ds)[0]+1]

		#
		# reduce spike times based on start/stop
		#spikeTimes0 = [spikeTime for spikeTime in spikeTimes0 if (spikeTime>=startPnt and spikeTime<=stopPnt)]
		spikeErrorList = [None] * len(spikeTimes0)

		#
		# throw out all spikes that are below a threshold Vm (usually below -20 mV)
		#spikeTimes0 = [spikeTime for spikeTime in spikeTimes0 if self.abf.sweepY[spikeTime] > self.mvThreshold]
		# 20190623 - already done in this vm threshold funtion
		'''
		peakWindow_ms = 10
		peakWindow_pnts = self.abf.dataPointsPerMs * peakWindow_ms
		goodSpikeTimes = []
		for spikeTime in spikeTimes0:
			peakVal = np.max(self.abf.sweepY[spikeTime:spikeTime+peakWindow_pnts])
			if peakVal > self.mvThreshold:
				goodSpikeTimes.append(spikeTime)
		spikeTimes0 = goodSpikeTimes
		'''

		#
		# throw out spike that are NOT upward deflections of Vm
		tmpLastGoodSpike_pnts = None
		#minISI_pnts = 5000 # at 20 kHz this is 0.25 sec
		minISI_ms = 75 #250
		minISI_pnts = self.ms2Pnt_(minISI_ms)

		prePntUp = 10 # pnts
		goodSpikeTimes = []
		goodSpikeErrors = []
		sweepY = self.sweepY(sweepNumber) # sweepNumber is not optional here
		for tmpIdx, spikeTime in enumerate(spikeTimes0):
			tmpFuckPreClip = sweepY[spikeTime-prePntUp:spikeTime]  # not including the stop index
			tmpFuckPostClip = sweepY[spikeTime+1:spikeTime+prePntUp+1]  # not including the stop index
			preAvg = np.average(tmpFuckPreClip)
			postAvg = np.average(tmpFuckPostClip)
			if postAvg > preAvg:
				tmpSpikeTimeSec = self.pnt2Sec_(spikeTime)
				if tmpLastGoodSpike_pnts is not None and (spikeTime-tmpLastGoodSpike_pnts) < minISI_pnts:
					continue
				goodSpikeTimes.append(spikeTime)
				goodSpikeErrors.append(spikeErrorList[tmpIdx])
				tmpLastGoodSpike_pnts = spikeTime
			else:
				tmpSpikeTimeSec = self.pnt2Sec_(spikeTime)

		# todo: add this to spikeDetect_dvdt()
		goodSpikeTimes, goodSpikeErrors = self._throwOutRefractory(goodSpikeTimes, goodSpikeErrors, refractory_ms=dDict['refractory_ms'])
		spikeTimes0 = goodSpikeTimes
		spikeErrorList = goodSpikeErrors

		#
		return spikeTimes0, spikeErrorList

	def spikeDetect(self, dDict=None):
		"""Spike Detet all sweeps.

		Each spike is a row and has 'sweep'
		"""
		startTime = time.time()
		#rememberSweep = self.currentSweep  # This is BAD we are mixing analysis with interface !!!

		self.spikeDict = [] # we are filling this in, one dict for each spike
		for sweepNumber in self.sweepList:
			#self.setSweep(sweep)
			self.spikeDetect__(sweepNumber, dDict=dDict)

		#
		#self.setSweep(rememberSweep)

		stopTime = time.time()
		logger.info(f'Detected {len(self.spikeDict)} spikes in {round(stopTime-startTime,3)} seconds')

	def spikeDetect__(self, sweepNumber, dDict=None):
		"""
		Spike detect the current sweep and put results into `self.spikeDict[]`.

		Args:
			dDict (dict): A detection dictionary from [bAnalysis.getDefaultDetection()][sanpy.bAnalysis.bAnalysis.getDefaultDetection]
		"""

		#logger.info('start detection')

		startTime = time.time()

		if dDict is None:
			dDict = self.detectionDict
			if dDict is None:
				dDict = bAnalysis.getDefaultDetection()

		self.detectionDict = dDict # remember the parameters of our last detection

		self.rebuildFiltered()

		# was this before adding detection per sweep
		#self.spikeDict = [] # we are filling this in, one dict for each spike

		#
		# spike detect
		detectionType = None
		if dDict['dvdtThreshold'] is None or np.isnan(dDict['dvdtThreshold']):
			# detect using mV threshold
			detectionType = 'mv'
			#self.spikeTimes, spikeErrorList = self._spikeDetect_vm(dDict)
			spikeTimes, spikeErrorList = self._spikeDetect_vm(dDict, sweepNumber)

			# backup childish vm threshold
			if dDict['doBackupSpikeVm']:
				#self.spikeTimes = self._backupSpikeVm(dDict['medianFilter'])
				spikeTimes = self._backupSpikeVm(spikeTimes, sweepNumber, dDict['medianFilter'])
		else:
			# detect using dv/dt threshold AND min mV
			detectionType = 'dvdt'
			#self.spikeTimes, spikeErrorList = self._spikeDetect_dvdt(dDict)
			spikeTimes, spikeErrorList = self._spikeDetect_dvdt(dDict, sweepNumber)

		#vm = self.filteredVm
		#dvdt = self.filteredDeriv
		sweepX = self.sweepX(sweepNumber)  # sweepNumber is not optional
		filteredVm = self.filteredVm(sweepNumber)  # sweepNumber is not optional
		filteredDeriv = self.filteredDeriv(sweepNumber)
		sweepC = self.sweepC(sweepNumber)

		#
		# look in a window after each threshold crossing to get AP peak
		peakWindow_pnts = self.dataPointsPerMs * dDict['peakWindow_ms']
		peakWindow_pnts = round(peakWindow_pnts)

		#
		# throw out spikes that have peak below onlyPeaksAbove_mV
		newSpikeTimes = []
		newSpikeErrorList = []
		if dDict['onlyPeaksAbove_mV'] is not None:
			for i, spikeTime in enumerate(spikeTimes):
				peakPnt = np.argmax(filteredVm[spikeTime:spikeTime+peakWindow_pnts])
				peakPnt += spikeTime
				peakVal = np.max(filteredVm[spikeTime:spikeTime+peakWindow_pnts])
				if peakVal > dDict['onlyPeaksAbove_mV']:
					newSpikeTimes.append(spikeTime)
					newSpikeErrorList.append(spikeErrorList[i])
				else:
					#print('spikeDetect() peak height: rejecting spike', i, 'at pnt:', spikeTime, "dDict['onlyPeaksAbove_mV']:", dDict['onlyPeaksAbove_mV'])
					pass
			spikeTimes = newSpikeTimes
			spikeErrorList = newSpikeErrorList

		#
		# throw out spikes on a down-slope
		avgWindow_pnts = dDict['avgWindow_ms'] * self.dataPointsPerMs
		avgWindow_pnts = math.floor(avgWindow_pnts/2)

		for i, spikeTime in enumerate(spikeTimes):
			# spikeTime units is ALWAYS points

			peakPnt = np.argmax(filteredVm[spikeTime:spikeTime+peakWindow_pnts])
			peakPnt += spikeTime
			peakVal = np.max(filteredVm[spikeTime:spikeTime+peakWindow_pnts])

			spikeDict = OrderedDict() # use OrderedDict so Pandas output is in the correct order

			spikeDict['include'] = 1
			spikeDict['analysisVersion'] = sanpy.analysisVersion
			spikeDict['interfaceVersion'] = sanpy.interfaceVersion
			spikeDict['file'] = self.getFileName()

			spikeDict['detectionType'] = detectionType
			spikeDict['cellType'] = dDict['cellType']
			spikeDict['sex'] = dDict['sex']
			spikeDict['condition'] = dDict['condition']

			spikeDict['sweep'] = sweepNumber
			# TODO: keep track of per sweep spike and total spike ???
			spikeDict['sweepSpikeNumber'] = i
			spikeDict['spikeNumber'] = self.numSpikes

			spikeDict['errors'] = []
			# append existing spikeErrorList from spikeDetect_dvdt() or spikeDetect_mv()
			tmpError = spikeErrorList[i]
			if tmpError is not None and tmpError != np.nan:
				#spikeDict['numError'] += 1
				spikeDict['errors'].append(tmpError) # tmpError is from:
							#eDict = self._getErrorDict(i, spikeTime, errType, errStr) # spikeTime is in pnts

			# detection params
			spikeDict['dvdtThreshold'] = dDict['dvdtThreshold']
			spikeDict['mvThreshold'] = dDict['mvThreshold']
			spikeDict['medianFilter'] = dDict['medianFilter']
			spikeDict['halfHeights'] = dDict['halfHeights']

			spikeDict['dacCommand'] = sweepC[spikeTime]  # spikeTime is in points

			spikeDict['thresholdPnt'] = spikeTime
			spikeDict['thresholdVal'] = filteredVm[spikeTime] # in vm
			spikeDict['thresholdVal_dvdt'] = filteredDeriv[spikeTime] # in dvdt, spikeTime is points
			spikeDict['thresholdSec'] = (spikeTime / self.dataPointsPerMs) / 1000

			spikeDict['peakPnt'] = peakPnt
			spikeDict['peakVal'] = peakVal
			spikeDict['peakSec'] = (peakPnt / self.dataPointsPerMs) / 1000

			spikeDict['peakHeight'] = spikeDict['peakVal'] - spikeDict['thresholdVal']

			#
			#
			# only append to spikeDict after we are done (accounting for spikes within a sweep)
			self.spikeDict.append(spikeDict)
			iIdx = len(self.spikeDict) - 1
			#
			#

			defaultVal = float('nan')

			# get pre/post spike minima
			self.spikeDict[iIdx]['preMinPnt'] = None
			self.spikeDict[iIdx]['preMinVal'] = defaultVal

			# early diastolic duration
			# 0.1 to 0.5 of time between pre spike min and spike time
			self.spikeDict[iIdx]['preLinearFitPnt0'] = None
			self.spikeDict[iIdx]['preLinearFitPnt1'] = None
			self.spikeDict[iIdx]['earlyDiastolicDuration_ms'] = defaultVal # seconds between preLinearFitPnt0 and preLinearFitPnt1
			self.spikeDict[iIdx]['preLinearFitVal0'] = defaultVal
			self.spikeDict[iIdx]['preLinearFitVal1'] = defaultVal
			# m,b = np.polyfit(x, y, 1)
			self.spikeDict[iIdx]['earlyDiastolicDurationRate'] = defaultVal # fit of y=preLinearFitVal 0/1 versus x=preLinearFitPnt 0/1
			self.spikeDict[iIdx]['lateDiastolicDuration'] = defaultVal #

			self.spikeDict[iIdx]['preSpike_dvdt_max_pnt'] = None
			self.spikeDict[iIdx]['preSpike_dvdt_max_val'] = defaultVal # in units mV
			self.spikeDict[iIdx]['preSpike_dvdt_max_val2'] = defaultVal # in units dv/dt
			self.spikeDict[iIdx]['postSpike_dvdt_min_pnt'] = None
			self.spikeDict[iIdx]['postSpike_dvdt_min_val'] = defaultVal # in units mV
			self.spikeDict[iIdx]['postSpike_dvdt_min_val2'] = defaultVal # in units dv/dt

			self.spikeDict[iIdx]['isi_pnts'] = defaultVal # time between successive AP thresholds (thresholdSec)
			self.spikeDict[iIdx]['isi_ms'] = defaultVal # time between successive AP thresholds (thresholdSec)
			self.spikeDict[iIdx]['spikeFreq_hz'] = defaultVal # time between successive AP thresholds (thresholdSec)
			self.spikeDict[iIdx]['cycleLength_pnts'] = defaultVal # time between successive MDPs
			self.spikeDict[iIdx]['cycleLength_ms'] = defaultVal # time between successive MDPs

			# Action potential duration (APD) was defined as the interval between the TOP and the subsequent MDP
			#self.spikeDict[iIdx]['apDuration_ms'] = defaultVal
			self.spikeDict[iIdx]['diastolicDuration_ms'] = defaultVal

			# any number of spike widths
			#print('spikeDetect__() appending widths list to spike iIdx:', iIdx)
			self.spikeDict[iIdx]['widths'] = []
			for halfHeight in dDict['halfHeights']:
				widthDict = {
					'halfHeight': halfHeight,
					'risingPnt': None,
					'risingVal': defaultVal,
					'fallingPnt': None,
					'fallingVal': defaultVal,
					'widthPnts': None,
					'widthMs': defaultVal
				}
				# abb 20210125, make column width_<n> where <n> is 'halfHeight'
				self.spikeDict[iIdx]['widths_' + str(halfHeight)] = defaultVal
				# was this
				self.spikeDict[iIdx]['widths'].append(widthDict)

			# The nonlinear late diastolic depolarization phase was estimated as the duration between 1% and 10% dV/dt
			# todo: not done !!!!!!!!!!

			# 20210413, was this. This next block is for pre spike analysis
			#			ok if we are at last spike
			#if i==0 or i==len(self.spikeTimes)-1:
			if i==0:
				# was continue but moved half width out of here
				pass
			else:
				mdp_ms = dDict['mdp_ms']
				mdp_pnts = mdp_ms * self.dataPointsPerMs
				#print('bAnalysis.spikeDetect() needs to be int mdp_pnts:', mdp_pnts)
				mdp_pnts = int(mdp_pnts)
				#
				# pre spike min
				#preRange = vm[self.spikeTimes[i-1]:self.spikeTimes[iIdx]]
				#startPnt = self.spikeTimes[iIdx]-mdp_pnts
				startPnt = spikeTimes[i]-mdp_pnts
				#print('  xxx preRange:', i, startPnt, self.spikeTimes[i])
				if startPnt<0:
					# for V-Clammp
					startPnt = 0
				#preRange = vm[startPnt:self.spikeTimes[i]] # EXCEPTION
				preRange = filteredVm[startPnt:spikeTimes[i]] # EXCEPTION
				preMinPnt = np.argmin(preRange)
				#preMinPnt += self.spikeTimes[i-1]
				preMinPnt += startPnt
				# the pre min is actually an average around the real minima
				avgRange = filteredVm[preMinPnt-avgWindow_pnts:preMinPnt+avgWindow_pnts]
				preMinVal = np.average(avgRange)

				# search backward from spike to find when vm reaches preMinVal (avg)
				#preRange = vm[preMinPnt:self.spikeTimes[i]]
				preRange = filteredVm[preMinPnt:spikeTimes[i]]
				preRange = np.flip(preRange) # we want to search backwards from peak
				try:
					preMinPnt2 = np.where(preRange<preMinVal)[0][0]
					#preMinPnt = self.spikeTimes[i] - preMinPnt2
					preMinPnt = spikeTimes[i] - preMinPnt2
					self.spikeDict[iIdx]['preMinPnt'] = preMinPnt
					self.spikeDict[iIdx]['preMinVal'] = preMinVal

				except (IndexError) as e:
					errorStr = 'searching for preMinVal:' + str(preMinVal) #+ ' postRange min:' + str(np.min(postRange)) + ' max ' + str(np.max(postRange))
					eDict = self._getErrorDict(i, spikeTimes[i], 'preMin', errorStr) # spikeTime is in pnts
					self.spikeDict[iIdx]['errors'].append(eDict)

				#
				# linear fit on 10% - 50% of the time from preMinPnt to self.spikeTimes[i]
				#print('spikeDetect__() linear fit spike i:', i)
				startLinearFit = 0.1 # percent of time between pre spike min and AP peak
				stopLinearFit = 0.5 # percent of time between pre spike min and AP peak
				# taking floor() so we always get an integer # points
				timeInterval_pnts = math.floor(spikeTimes[i] - preMinPnt)
				preLinearFitPnt0 = preMinPnt + math.floor(timeInterval_pnts * startLinearFit)
				preLinearFitPnt1 = preMinPnt + math.floor(timeInterval_pnts * stopLinearFit)
				preLinearFitVal0 = filteredVm[preLinearFitPnt0]
				preLinearFitVal1 = filteredVm[preLinearFitPnt1]

				# linear fit before spike
				self.spikeDict[iIdx]['preLinearFitPnt0'] = preLinearFitPnt0
				self.spikeDict[iIdx]['preLinearFitPnt1'] = preLinearFitPnt1
				self.spikeDict[iIdx]['earlyDiastolicDuration_ms'] = self.pnt2Ms_(preLinearFitPnt1 - preLinearFitPnt0)
				self.spikeDict[iIdx]['preLinearFitVal0'] = preLinearFitVal0
				self.spikeDict[iIdx]['preLinearFitVal1'] = preLinearFitVal1

				# a linear fit where 'm,b = np.polyfit(x, y, 1)'
				# m*x+b"
				xFit = sweepX[preLinearFitPnt0:preLinearFitPnt1]
				yFit = filteredVm[preLinearFitPnt0:preLinearFitPnt1]
				with warnings.catch_warnings():
					warnings.filterwarnings('error')
					try:
						mLinear, bLinear = np.polyfit(xFit, yFit, 1) # m is slope, b is intercept
						self.spikeDict[iIdx]['earlyDiastolicDurationRate'] = mLinear
					except TypeError:
						#catching exception: raise TypeError("expected non-empty vector for x")
						print('TypeError')
						self.spikeDict[iIdx]['earlyDiastolicDurationRate'] = defaultVal
						errorStr = 'earlyDiastolicDurationRate fit'
						eDict = self._getErrorDict(i, spikeTimes[i], 'fitEDD', errorStr) # spikeTime is in pnts
						self.spikeDict[iIdx]['errors'].append(eDict)
					except np.RankWarning:
						print('RankWarning')
						# also throws: RankWarning: Polyfit may be poorly conditioned
						self.spikeDict[iIdx]['earlyDiastolicDurationRate'] = defaultVal
						errorStr = 'earlyDiastolicDurationRate fit - RankWarning'
						eDict = self._getErrorDict(i, spikeTimes[i], 'fitEDD2', errorStr) # spikeTime is in pnts
						self.spikeDict[iIdx]['errors'].append(eDict)
					except:
						print(' !!!!!!!!!!!!!!!!!!!!!!!!!!! EXCEPTION DURING LINEAR FIT')

				# not implemented
				#self.spikeDict[i]['lateDiastolicDuration'] = ???

			#
			# maxima in dv/dt before spike
			# added try/except sunday april 14, seems to break spike detection???
			try:
				# 20210415 was this
				#preRange = dvdt[self.spikeTimes[i]:peakPnt]
				#preRange = dvdt[self.spikeTimes[i]:peakPnt+1]
				preRange = filteredDeriv[spikeTimes[i]:peakPnt+1]
				preSpike_dvdt_max_pnt = np.argmax(preRange)
				#preSpike_dvdt_max_pnt += self.spikeTimes[i]
				preSpike_dvdt_max_pnt += spikeTimes[i]
				self.spikeDict[iIdx]['preSpike_dvdt_max_pnt'] = preSpike_dvdt_max_pnt
				self.spikeDict[iIdx]['preSpike_dvdt_max_val'] = filteredVm[preSpike_dvdt_max_pnt] # in units mV
				self.spikeDict[iIdx]['preSpike_dvdt_max_val2'] = filteredDeriv[preSpike_dvdt_max_pnt] # in units mV
			except (ValueError) as e:
				#self.spikeDict[iIdx]['numError'] = self.spikeDict[iIdx]['numError'] + 1
				# sometimes preRange is empty, don't try and put min/max in error
				errorStr = 'searching for preSpike_dvdt_max_pnt:'
				eDict = self._getErrorDict(i, spikeTimes[i], 'preSpikeDvDt', errorStr) # spikeTime is in pnts
				self.spikeDict[iIdx]['errors'].append(eDict)

			# 20210501, we do not need postMin/mdp, not used anywhere else
			'''
			if i==len(self.spikeTimes)-1:
				# last spike
				pass
			else:
				#
				# post spike min
				postRange = vm[self.spikeTimes[i]:self.spikeTimes[i+1]]
				postMinPnt = np.argmin(postRange)
				postMinPnt += self.spikeTimes[i]
				# the post min is actually an average around the real minima
				avgRange = vm[postMinPnt-avgWindow_pnts:postMinPnt+avgWindow_pnts]
				postMinVal = np.average(avgRange)

				# search forward from spike to find when vm reaches postMinVal (avg)
				postRange = vm[self.spikeTimes[i]:postMinPnt]
				try:
					postMinPnt2 = np.where(postRange<postMinVal)[0][0]
					postMinPnt = self.spikeTimes[i] + postMinPnt2
					self.spikeDict[i]['postMinPnt'] = postMinPnt
					self.spikeDict[i]['postMinVal'] = postMinVal
				except (IndexError) as e:
					self.spikeDict[i]['numError'] = self.spikeDict[i]['numError'] + 1
					# sometimes postRange is empty, don't try and put min/max in error
					#print('postRange:', postRange)
					errorStr = 'searching for postMinVal:' + str(postMinVal) #+ ' postRange min:' + str(np.min(postRange)) + ' max ' + str(np.max(postRange))
					eDict = self._getErrorDict(i, self.spikeTimes[i], 'postMinError', errorStr) # spikeTime is in pnts
					self.spikeDict[i]['errors'].append(eDict)
			'''

			if True:
				#
				# minima in dv/dt after spike
				#postRange = dvdt[self.spikeTimes[i]:postMinPnt]
				postSpike_ms = 10
				postSpike_pnts = self.dataPointsPerMs * postSpike_ms
				# abb 20210130 lcr analysis
				postSpike_pnts = round(postSpike_pnts)
				#postRange = dvdt[self.spikeTimes[i]:self.spikeTimes[i]+postSpike_pnts] # fixed window after spike
				postRange = filteredDeriv[peakPnt:peakPnt+postSpike_pnts] # fixed window after spike

				postSpike_dvdt_min_pnt = np.argmin(postRange)
				postSpike_dvdt_min_pnt += peakPnt
				self.spikeDict[iIdx]['postSpike_dvdt_min_pnt'] = postSpike_dvdt_min_pnt
				self.spikeDict[iIdx]['postSpike_dvdt_min_val'] = filteredVm[postSpike_dvdt_min_pnt]
				self.spikeDict[iIdx]['postSpike_dvdt_min_val2'] = filteredDeriv[postSpike_dvdt_min_pnt]

				# 202102
				#self.spikeDict[iIdx]['preMinPnt'] = preMinPnt
				#self.spikeDict[iIdx]['preMinVal'] = preMinVal
				# 202102
				#self.spikeDict[iIdx]['postMinPnt'] = postMinPnt
				#self.spikeDict[iIdx]['postMinVal'] = postMinVal

				# linear fit before spike
				#self.spikeDict[iIdx]['preLinearFitPnt0'] = preLinearFitPnt0
				#self.spikeDict[iIdx]['preLinearFitPnt1'] = preLinearFitPnt1
				#self.spikeDict[iIdx]['earlyDiastolicDuration_ms'] = self.pnt2Ms_(preLinearFitPnt1 - preLinearFitPnt0)
				#self.spikeDict[iIdx]['preLinearFitVal0'] = preLinearFitVal0
				#self.spikeDict[iIdx]['preLinearFitVal1'] = preLinearFitVal1

				#
				# Action potential duration (APD) was defined as
				# the interval between the TOP and the subsequent MDP
				# 20210501, removed AP duration, use APD_90, APD_50 etc
				'''
				if i==len(self.spikeTimes)-1:
					pass
				else:
					self.spikeDict[iIdx]['apDuration_ms'] = self.pnt2Ms_(postMinPnt - spikeDict['thresholdPnt'])
				'''

				#
				# diastolic duration was defined as
				# the interval between MDP and TOP
				if i > 0:
					# one off error when preMinPnt is not defined
					self.spikeDict[iIdx]['diastolicDuration_ms'] = self.pnt2Ms_(spikeTime - preMinPnt)

				self.spikeDict[iIdx]['cycleLength_ms'] = float('nan')
				if i>0: #20190627, was i>1
					isiPnts = self.spikeDict[iIdx]['thresholdPnt'] - self.spikeDict[iIdx-1]['thresholdPnt']
					isi_ms = self.pnt2Ms_(isiPnts)
					isi_hz = 1 / (isi_ms / 1000)
					self.spikeDict[iIdx]['isi_pnts'] = isiPnts
					self.spikeDict[iIdx]['isi_ms'] = self.pnt2Ms_(isiPnts)
					self.spikeDict[iIdx]['spikeFreq_hz'] = 1 / (self.pnt2Ms_(isiPnts) / 1000)

					# Cycle length was defined as the interval between MDPs in successive APs
					prevPreMinPnt = self.spikeDict[iIdx-1]['preMinPnt'] # can be nan
					thisPreMinPnt = self.spikeDict[iIdx]['preMinPnt']
					if prevPreMinPnt is not None and thisPreMinPnt is not None:
						cycleLength_pnts = thisPreMinPnt - prevPreMinPnt
						self.spikeDict[iIdx]['cycleLength_pnts'] = cycleLength_pnts
						self.spikeDict[iIdx]['cycleLength_ms'] = self.pnt2Ms_(cycleLength_pnts)
					else:
						# error
						#self.spikeDict[iIdx]['numError'] = self.spikeDict[iIdx]['numError'] + 1
						errorStr = 'previous spike preMinPnt is ' + str(prevPreMinPnt) + ' this preMinPnt:' + str(thisPreMinPnt)
						eDict = self._getErrorDict(i, spikeTimes[i], 'cycleLength', errorStr) # spikeTime is in pnts
						self.spikeDict[iIdx]['errors'].append(eDict)
					'''
					# 20210501 was this, I am no longer using postMinPnt
					prevPostMinPnt = self.spikeDict[i-1]['postMinPnt']
					tmpPostMinPnt = self.spikeDict[iIdx]['postMinPnt']
					if prevPostMinPnt is not None and tmpPostMinPnt is not None:
						cycleLength_pnts = tmpPostMinPnt - prevPostMinPnt
						self.spikeDict[iIdx]['cycleLength_pnts'] = cycleLength_pnts
						self.spikeDict[iIdx]['cycleLength_ms'] = self.pnt2Ms_(cycleLength_pnts)
					else:
						self.spikeDict[iIdx]['numError'] = self.spikeDict[iIdx]['numError'] + 1
						errorStr = 'previous spike postMinPnt is ' + str(prevPostMinPnt) + ' this postMinPnt:' + str(tmpPostMinPnt)
						eDict = self._getErrorDict(i, self.spikeTimes[i], 'cycleLength', errorStr) # spikeTime is in pnts
						self.spikeDict[iIdx]['errors'].append(eDict)
					'''
			#
			# spike half with and APDur
			#

			#
			# 20210130, moving 'half width' out of inner if spike # is first/last
			#
			# get 1/2 height (actually, any number of height measurements)
			# action potential duration using peak and post min
			#self.spikeDict[i]['widths'] = []
			#print('*** calculating half width for spike', i)

			#
			# TODO: move this to a function
			#

			doWidthVersion2 = False

			#halfWidthWindow_ms = 20
			hwWindowPnts = dDict['halfWidthWindow_ms'] * self.dataPointsPerMs
			hwWindowPnts = round(hwWindowPnts)

			tmpPeakSec = spikeDict['peakSec']
			tmpErrorType = None
			for j, halfHeight in enumerate(dDict['halfHeights']):
				# halfHeight in [20, 50, 80]
				if doWidthVersion2:
					tmpThreshVm = spikeDict['thresholdVal']
					thisVm = tmpThreshVm + (peakVal - tmpThreshVm) * (halfHeight * 0.01)
				else:
					# 20210413 was this
					#thisVm = postMinVal + (peakVal - postMinVal) * (halfHeight * 0.01)
					tmpThreshVm2 = spikeDict['thresholdVal']
					thisVm = tmpThreshVm2 + (peakVal - tmpThreshVm2) * (halfHeight * 0.01)
				#todo: logic is broken, this get over-written in following try
				widthDict = {
					'halfHeight': halfHeight,
					'risingPnt': None,
					'risingVal': defaultVal,
					'fallingPnt': None,
					'fallingVal': defaultVal,
					'widthPnts': None,
					'widthMs': defaultVal
				}
				widthMs = np.nan
				try:
					if doWidthVersion2:
						postRange = filteredVm[peakPnt:peakPnt+hwWindowPnts]
					else:
						# 20210413 was this
						#postRange = filteredVm[peakPnt:postMinPnt]
						postRange = filteredVm[peakPnt:peakPnt+hwWindowPnts]
					fallingPnt = np.where(postRange<thisVm)[0] # less than
					if len(fallingPnt)==0:
						#error
						tmpErrorType = 'falling point'
						raise IndexError
					fallingPnt = fallingPnt[0] # first falling point
					fallingPnt += peakPnt
					fallingVal = filteredVm[fallingPnt]

					# use the post/falling to find pre/rising
					if doWidthVersion2:
						preRange = filteredVm[peakPnt-hwWindowPnts:peakPnt]
					else:
						tmpPreMinPnt2 = spikeDict['thresholdPnt']
						preRange = filteredVm[tmpPreMinPnt2:peakPnt]
					risingPnt = np.where(preRange>fallingVal)[0] # greater than
					if len(risingPnt)==0:
						#error
						tmpErrorType = 'rising point'
						raise IndexError
					risingPnt = risingPnt[0] # first falling point

					if doWidthVersion2:
						risingPnt += peakPnt-hwWindowPnts
					else:
						risingPnt += spikeDict['thresholdPnt']
					risingVal = filteredVm[risingPnt]

					# width (pnts)
					widthPnts = fallingPnt - risingPnt
					# 20210413
					widthPnts2 = fallingPnt - spikeDict['thresholdPnt']
					tmpRisingPnt = spikeDict['thresholdPnt']
					# assign
					widthDict['halfHeight'] = halfHeight
					# 20210413, put back in
					#widthDict['risingPnt'] = risingPnt
					widthDict['risingPnt'] = tmpRisingPnt
					widthDict['risingVal'] = risingVal
					widthDict['fallingPnt'] = fallingPnt
					widthDict['fallingVal'] = fallingVal
					widthDict['widthPnts'] = widthPnts
					widthDict['widthMs'] = widthPnts / self.dataPointsPerMs
					widthMs = widthPnts / self.dataPointsPerMs # abb 20210125
					# 20210413, todo: make these end in 2
					widthDict['widthPnts'] = widthPnts2
					widthDict['widthMs'] = widthPnts / self.dataPointsPerMs

				except (IndexError) as e:
					##
					##
					#print('  EXCEPTION: bAnalysis.spikeDetect() spike', i, 'half height', halfHeight)
					##
					##

					#self.spikeDict[iIdx]['numError'] = self.spikeDict[iIdx]['numError'] + 1
					#errorStr = 'spike ' + str(i) + ' half width ' + str(tmpErrorType) + ' ' + str(halfHeight) + ' halfWidthWindow_ms:' + str(dDict['halfWidthWindow_ms'])
					errorStr = (f'half width {halfHeight} error in {tmpErrorType} '
							f"with halfWidthWindow_ms:{dDict['halfWidthWindow_ms']} "
							f'searching for Vm:{round(thisVm,2)} from peak sec {round(tmpPeakSec,2)}'
							)

					eDict = self._getErrorDict(i, spikeTimes[i], 'spikeWidth', errorStr) # spikeTime is in pnts
					self.spikeDict[i]['errors'].append(eDict)

				# abb 20210125
				# wtf is hapenning on Heroku????
				#print('**** heroku debug i:', i, 'j:', j, 'len:', len(self.spikeDict), 'halfHeight:', halfHeight)
				#print('  self.spikeDict[i]', self.spikeDict[i]['widths_'+str(halfHeight)])

				self.spikeDict[iIdx]['widths_'+str(halfHeight)] = widthMs
				self.spikeDict[iIdx]['widths'][j] = widthDict

		#
		# look between threshold crossing to get minima
		# we will ignore the first and last spike

		#
		# spike clips
		self.spikeClips = None
		self.spikeClips_x = None
		self.spikeClips_x2 = None
		'''
		spikeClipWidth_ms = dDict['spikeClipWidth_ms']
		#clipStartSec = dDict['startSeconds']
		#clipStopSec = dDict['stopSeconds']
		#theseTime_sec = [clipStartSec, clipStopSec]
		theseTime_sec = None
		self._makeSpikeClips(spikeClipWidth_ms, theseTime_sec=theseTime_sec)
		'''

		# TODO: Remove this comment block
		'''
		clipWidth_pnts = dDict['spikeClipWidth_ms'] * self.dataPointsPerMs
		clipWidth_pnts = round(clipWidth_pnts)
		if clipWidth_pnts % 2 == 0:
			pass # Even
		else:
			clipWidth_pnts += 1 # Odd

		halfClipWidth_pnts = int(clipWidth_pnts/2)

		#print('  spikeDetect() clipWidth_pnts:', clipWidth_pnts, 'halfClipWidth_pnts:', halfClipWidth_pnts)
		# make one x axis clip with the threshold crossing at 0
		self.spikeClips_x = [(x-halfClipWidth_pnts)/self.dataPointsPerMs for x in range(clipWidth_pnts)]

		#20190714, added this to make all clips same length, much easier to plot in MultiLine
		numPointsInClip = len(self.spikeClips_x)

		self.spikeClips = []
		self.spikeClips_x2 = []
		for idx, spikeTime in enumerate(self.spikeTimes):
			#currentClip = vm[spikeTime-halfClipWidth_pnts:spikeTime+halfClipWidth_pnts]
			currentClip = vm[spikeTime-halfClipWidth_pnts:spikeTime+halfClipWidth_pnts]
			if len(currentClip) == numPointsInClip:
				self.spikeClips.append(currentClip)
				self.spikeClips_x2.append(self.spikeClips_x) # a 2D version to make pyqtgraph multiline happy
			else:
				##
				##
				if idx==0 or idx==len(self.spikeTimes)-1:
					# don't report spike clip errors for first/last spike
					pass
				else:
					print('  ERROR: bAnalysis.spikeDetect() did not add clip for spike index', idx, 'at time', spikeTime, 'currentClip:', len(currentClip), 'numPointsInClip:', numPointsInClip)
				##
				##
		'''
		# 20210426
		# generate a df holding stats (used by scatterplotwidget)
		startSeconds = dDict['startSeconds']
		stopSeconds = dDict['stopSeconds']
		#tmpAnalysisName, df0 = self.getReportDf(theMin, theMax, savefile)
		if self.numSpikes > 0:
			exportObject = sanpy.bExport(self)
			self.dfReportForScatter = exportObject.report(startSeconds, stopSeconds)
		else:
			self.dfReportForScatter = None

		self.dfError = self.errorReport()

		self._detectionDirty = True

		stopTime = time.time()
		#print('bAnalysis.spikeDetect() for file', self.getFileName())
		#logger.info(f'Detected {len(spikeTimes)} spikes in {round(stopTime-startTime,3)} seconds')

		#return self.dfReportForScatter

	def _makeSpikeClips(self, spikeClipWidth_ms=None, theseTime_sec=None, sweepNumber=None):
		"""
		(Internal) Make small clips for each spike.

		Args:
			spikeClipWidth_ms (int): Width of each spike clip in milliseconds.
			theseTime_sec (list of float): [NOT USED] List of seconds to make clips from.

		Returns:
			spikeClips_x2: ms
			self.spikeClips (list): List of spike clips
		"""

		if spikeClipWidth_ms is None:
			spikeClipWidth_ms = self.detectionDict['spikeClipWidth_ms']

		if sweepNumber is None:
			sweepNumber = 'All'

		#print('makeSpikeClips() spikeClipWidth_ms:', spikeClipWidth_ms, 'theseTime_sec:', theseTime_sec)
		if theseTime_sec is None:
			theseTime_pnts = self.getSpikeTimes(sweepNumber=sweepNumber)
		else:
			# convert theseTime_sec to pnts
			theseTime_ms = [x*1000 for x in theseTime_sec]
			theseTime_pnts = [x*self.dataPointsPerMs for x in theseTime_ms]
			theseTime_pnts = [round(x) for x in theseTime_pnts]

		clipWidth_pnts = spikeClipWidth_ms * self.dataPointsPerMs
		clipWidth_pnts = round(clipWidth_pnts)
		if clipWidth_pnts % 2 == 0:
			pass # Even
		else:
			clipWidth_pnts += 1 # Make odd even

		halfClipWidth_pnts = int(clipWidth_pnts/2)

		#print('  makeSpikeClips() clipWidth_pnts:', clipWidth_pnts, 'halfClipWidth_pnts:', halfClipWidth_pnts)
		# make one x axis clip with the threshold crossing at 0
		self.spikeClips_x = [(x-halfClipWidth_pnts)/self.dataPointsPerMs for x in range(clipWidth_pnts)]

		#20190714, added this to make all clips same length, much easier to plot in MultiLine
		numPointsInClip = len(self.spikeClips_x)

		self.spikeClips = []
		self.spikeClips_x2 = []

		#for idx, spikeTime in enumerate(self.spikeTimes):
		sweepY = self.sweepY(sweepNumber=sweepNumber)
		sweepNum = self.getStat('sweep', sweepNumber=sweepNumber)  # For 'All' sweeps, we need to know column

		logger.info(f'sweepY: {sweepY.shape} {len(sweepY.shape)}')
		logger.info(f'theseTime_pnts: {theseTime_pnts}')

		for idx, spikeTime in enumerate(theseTime_pnts):

			sweep = sweepNum[idx]

			if len(sweepY.shape) == 1:
				# 1D case where recording has only oone sweep
				currentClip = sweepY[spikeTime-halfClipWidth_pnts:spikeTime+halfClipWidth_pnts]
			else:
				# 2D case where recording has multiple sweeps
				currentClip = sweepY[spikeTime-halfClipWidth_pnts:spikeTime+halfClipWidth_pnts, sweep]

			if len(currentClip) == numPointsInClip:
				self.spikeClips.append(currentClip)
				self.spikeClips_x2.append(self.spikeClips_x) # a 2D version to make pyqtgraph multiline happy
			else:
				#pass
				logger.error(f'Did not add clip for spike index: {idx} at time: {spikeTime} len(currentClip): {len(currentClip)} != numPointsInClip: {numPointsInClip}')

		#
		return self.spikeClips_x2, self.spikeClips

	def getSpikeClips(self, theMin, theMax, spikeClipWidth_ms=None, sweepNumber=None):
		"""
		get 2d list of spike clips, spike clips x, and 1d mean spike clip

		Args:
			theMin (float): Start seconds.
			theMax (float): Stop seconds.

		Requires: self.spikeDetect() and self._makeSpikeClips()
		"""

		if self.numSpikes == 0:
			return

		if theMin is None or theMax is None:
			theMin = 0
			theMax = self.recordingDur  # self.sweepX[-1]

		# new interface, spike detect no longer auto generates these
		# need to do this every time because we get here when sweepNumber changes
		#if self.spikeClips is None:
		#	self._makeSpikeClips(spikeClipWidth_ms=spikeClipWidth_ms, sweepNumber=sweepNumber)
		self._makeSpikeClips(spikeClipWidth_ms=spikeClipWidth_ms, sweepNumber=sweepNumber)

		#logger.info(f'self.spikeClips: {self.spikeClips}')

		# make a list of clips within start/stop (Seconds)
		theseClips = []
		theseClips_x = []
		tmpMeanClips = [] # for mean clip
		meanClip = []
		spikeTimes = self.getSpikeTimes(sweepNumber=sweepNumber)

		if len(spikeTimes) != len(self.spikeClips):
			print(f'ERROR getSpikeClips() len spikeTimes !=  spikeClips {len(spikeTimes)} {len(self.spikeClips)}')

		# self.spikeClips is a list of clips
		for idx, clip in enumerate(self.spikeClips):
			spikeTime = spikeTimes[idx]
			spikeTime = self.pnt2Sec_(spikeTime)
			if spikeTime>=theMin and spikeTime<=theMax:
				theseClips.append(clip)
				theseClips_x.append(self.spikeClips_x2[idx]) # remember, all _x are the same
				if len(self.spikeClips_x) == len(clip):
					tmpMeanClips.append(clip) # for mean clip
		if len(tmpMeanClips):
			meanClip = np.mean(tmpMeanClips, axis=0)

		return theseClips, theseClips_x, meanClip

	def numErrors(self):
		if self.dfError is None:
			return 'N/A'
		else:
			return len(self.dfError)

	def errorReport(self):
		"""
		Generate an error report, one row per error. Spikes can have more than one error.

		Returns:
			(pandas DataFrame): Pandas DataFrame, one row per error.
		"""

		dictList = []

		numError = 0
		errorList = []
		for spikeIdx, spike in enumerate(self.spikeDict):
			for idx, error in enumerate(spike['errors']):
				# error is dict from _getErorDict
				if error is None or error == np.nan or error == 'nan':
					continue
				dictList.append(error)

		if len(dictList) == 0:
			fakeErrorDict = self._getErrorDict(1, 1, 'fake', 'fake')
			dfError = pd.DataFrame(columns=fakeErrorDict.keys())
		else:
			dfError = pd.DataFrame(dictList)

		#print('bAnalysis.errorReport() returning len(dfError):', len(dfError))
		if self.detectionDict['verbose']:
			logger.info(f'Found {len(dfError)} errors in spike detection')

		#
		return dfError

	def save_csv(self):
		"""
		Save as a CSV text file with name <path>_analysis.csv'

		TODO: Fix
		TODO: We need to save header with xxx
		TODO: Load <path>_analysis.csv
		"""
		savefile = os.path.splitext(self._path)[0]
		savefile += '_analysis.csv'
		saveExcel = False
		alsoSaveTxt = True
		logger.info(f'Saving "{savefile}"')

		be = sanpy.bExport(self)
		be.saveReport(savefile, saveExcel=saveExcel, alsoSaveTxt=alsoSaveTxt)

	def pnt2Sec_(self, pnt):
		"""
		Convert a point to Seconds using `self.dataPointsPerMs`

		Args:
			pnt (int): The point

		Returns:
			float: The point in seconds
		"""
		if pnt is None:
			return math.isnan(pnt)
		else:
			return pnt / self.dataPointsPerMs / 1000

	def pnt2Ms_(self, pnt):
		"""
		Convert a point to milliseconds (ms) using `self.dataPointsPerMs`

		Args:
			pnt (int): The point

		Returns:
			float: The point in milliseconds (ms)
		"""
		return pnt / self.dataPointsPerMs

	def ms2Pnt_(self, ms):
		"""
		Convert milliseconds (ms) to point in recording using `self.dataPointsPerMs`

		Args:
			ms (float): The ms into the recording

		Returns:
			int: The point in the recording
		"""
		theRet = ms * self.dataPointsPerMs
		theRet = int(theRet)
		return theRet

	def _normalizeData(self, data):
		"""
		Used to calculate normalized data for detection from Kymograph. Is NOT for df/d0.
		"""
		return (data - np.min(data)) / (np.max(data) - np.min(data))

	def api_getHeader(self):
		"""
		Get header as a dict.

		TODO:
			- add info on abf file, like samples per ms

		Returns:
			dict: Dictionary of information about loaded file.
		"""
		recordingDir_sec = len(self.sweepX) / self.dataPointsPerMs / 1000
		recordingFrequency = self.dataPointsPerMs

		ret = {
			'myFileType': self.myFileType, # ('abf', 'tif', 'bytestream', 'csv')
			'loadError': self.loadError,
			'detectionDict': self.detectionDict,
			'path': self._path,
			'file': self.getFileName(),
			'dateAnalyzed': self.dateAnalyzed,
			'detectionType': self.detectionType,
			'acqDate': self.acqDate,
			'acqTime': self.acqTime,
			#
			'_recordingMode': self._recordingMode,
			'get_yUnits': self.get_yUnits,
			#'currentSweep': self.currentSweep,
			'recording_kHz': recordingFrequency,
			'recordingDur_sec': recordingDir_sec
		}
		return ret

	def api_getSpikeInfo(self, spikeNum=None):
		"""
		Get info about each spike.

		Args:
			spikeNum (int): Get info for one spike, None for all spikes.

		Returns:
			list: List of dict with info for all (one) spike.
		"""
		if spikeNum is not None:
			ret = [self.spikeDict[spikeNum]]
		else:
			ret = self.spikeDict
		return ret

	def api_getSpikeStat(self, stat):
		"""
		Get stat for each spike

		Args:
			stat (str): The name of the stat to get. Corresponds to key in self.spikeDict[i].

		Returns:
			list: List of values for 'stat'. Ech value is for one spike.
		"""
		statList = self.getStat(statName1=stat, statName2=None)
		return statList

	def api_getRecording(self):
		"""
		Return primary recording

		Returns:
			dict: {'header', 'sweepX', 'sweepY'}

		TODO:
			Add param to only get every n'th point, to return a subset faster (for display)
		"""
		#start = time.time()
		ret = {
			'header': self.api_getHeader(),
			'sweepX': self.sweepX,
			'sweepY': self.sweepY,
		}
		#stop = time.time()
		#print(stop-start)
		return ret

	def openHeaderInBrowser(self):
		"""Open abf file header in browser. Only works for actual abf files."""
		#ba.abf.headerLaunch()
		if self.abf is None:
			return
		import webbrowser
		logFile = sanpy.sanpyLogger.getLoggerFile()
		htmlFile = os.path.splitext(logFile)[0] + '.html'
		#print('htmlFile:', htmlFile)
		html = pyabf.abfHeaderDisplay.abfInfoPage(self.abf).generateHTML()
		with open(htmlFile, 'w') as f:
			f.write(html)
		webbrowser.open('file://' + htmlFile)

def _test_load_abf():
	path = '/Users/cudmore/data/dual-lcr/20210115/data/21115002.abf'
	ba = bAnalysis(path)

	dvdtThreshold = 30
	mvThreshold = -20
	halfWidthWindow_ms = 60 # was 20
	ba.spikeDetect(dvdtThreshold=dvdtThreshold, mvThreshold=mvThreshold,
		halfWidthWindow_ms=halfWidthWindow_ms
		)
		#avgWindow_ms=avgWindow_ms,
		#window_ms=window_ms,
		#peakWindow_ms=peakWindow_ms,
		#refractory_ms=refractory_ms,
		#dvdt_percentOfMax=dvdt_percentOfMax)

	test_plot(ba)

	return ba

def _test_load_tif(path):
	"""
	working on spike detection from sum of intensities along a line scan
	see: example/lcr-analysis
	"""

	# text file with 2x columns (seconds, vm)

	#path = '/Users/Cudmore/Desktop/caInt.csv'

	#path = '/Users/cudmore/data/dual-lcr/20210115/data/20210115__0001.tif'

	ba = bAnalysis(path)
	ba.getDerivative()

	#print('ba.abf.sweepX:', len(ba.abf.sweepX))
	#print('ba.abf.sweepY:', len(ba.abf.sweepY))

	#print('ba.abf.sweepX:', ba.abf.sweepX)
	#print('ba.abf.sweepY:', ba.abf.sweepY)

	# Ca recording is ~40 times slower than e-phys at 10 kHz
	mvThreshold = 0.5
	dvdtThreshold = 0.01
	refractory_ms = 60 # was 20 ms
	avgWindow_ms=60 # pre-roll to find eal threshold crossing
		# was 5, in detect I am using avgWindow_ms/2 ???
	window_ms = 20 # was 2
	peakWindow_ms = 70 # 20 gives us 5, was 10
	dvdt_percentOfMax = 0.2 # was 0.1
	halfWidthWindow_ms = 60 # was 20
	ba.spikeDetect(dvdtThreshold=dvdtThreshold, mvThreshold=mvThreshold,
		avgWindow_ms=avgWindow_ms,
		window_ms=window_ms,
		peakWindow_ms=peakWindow_ms,
		refractory_ms=refractory_ms,
		dvdt_percentOfMax=dvdt_percentOfMax,
		halfWidthWindow_ms=halfWidthWindow_ms
		)

	for k,v in ba.spikeDict[0].items():
		print('  ', k, ':', v)

	test_plot(ba)

	return ba

def _test_plot(ba, firstSampleTime=0):
	#firstSampleTime = ba.abf.sweepX[0] # is not 0 for 'wait for trigger' FV3000

	# plot
	fig, axs = plt.subplots(2, 1, sharex=True)

	#
	# dv/dt
	xDvDt = ba.abf.sweepX + firstSampleTime
	yDvDt = ba.abf.filteredDeriv + firstSampleTime
	axs[0].plot(xDvDt, yDvDt, 'k')

	# thresholdVal_dvdt
	xThresh = [x['thresholdSec'] + firstSampleTime for x in ba.spikeDict]
	yThresh = [x['thresholdVal_dvdt'] for x in ba.spikeDict]
	axs[0].plot(xThresh, yThresh, 'or')

	axs[0].spines['right'].set_visible(False)
	axs[0].spines['top'].set_visible(False)

	#
	# vm with detection params
	axs[1].plot(ba.abf.sweepX, ba.abf.sweepY, 'k-', lw=0.5)

	xThresh = [x['thresholdSec'] + firstSampleTime for x in ba.spikeDict]
	yThresh = [x['thresholdVal'] for x in ba.spikeDict]
	axs[1].plot(xThresh, yThresh, 'or')

	xPeak = [x['peakSec'] + firstSampleTime for x in ba.spikeDict]
	yPeak = [x['peakVal'] for x in ba.spikeDict]
	axs[1].plot(xPeak, yPeak, 'ob')

	sweepX = ba.abf.sweepX + firstSampleTime

	for idx, spikeDict in enumerate(ba.spikeDict):
		#
		# plot all widths
		#print('plotting width for spike', idx)
		for j,widthDict in enumerate(spikeDict['widths']):
			#for k,v in widthDict.items():
			#	print('  ', k, ':', v)
			#print('j:', j)
			if widthDict['risingPnt'] is None:
				#print('  -->> no half width')
				continue

			risingPntX = sweepX[widthDict['risingPnt']]
			# y value of rising pnt is y value of falling pnt
			#risingPntY = ba.abf.sweepY[widthDict['risingPnt']]
			risingPntY = ba.abf.sweepY[widthDict['fallingPnt']]
			fallingPntX = sweepX[widthDict['fallingPnt']]
			fallingPntY = ba.abf.sweepY[widthDict['fallingPnt']]
			fallingPnt = widthDict['fallingPnt']
			# plotting y-value of rising to match y-value of falling
			#ax.plot(ba.abf.sweepX[widthDict['risingPnt']], ba.abf.sweepY[widthDict['risingPnt']], 'ob')
			# plot as pnts
			#axs[1].plot(ba.abf.sweepX[widthDict['risingPnt']], ba.abf.sweepY[widthDict['fallingPnt']], '-b')
			#axs[1].plot(ba.abf.sweepX[widthDict['fallingPnt']], ba.abf.sweepY[widthDict['fallingPnt']], '-b')
			# line between rising and falling is ([x1, y1], [x2, y2])
			axs[1].plot([risingPntX, fallingPntX], [risingPntY, fallingPntY], color='b', linestyle='-', linewidth=2)

	axs[1].spines['right'].set_visible(False)
	axs[1].spines['top'].set_visible(False)

	#
	#plt.show()

def _lcrDualAnalysis():
	"""
	for 2x files, line-scan and e-phys
	plot spike time delay of ca imaging
	"""
	fileIndex = 3
	dataList[fileIndex]

	ba0 = test_load_tif(path) # image
	ba1 = test_load_abf() # recording

	# now need to get this from pClamp abf !!!
	#firstSampleTime = ba0.abf.sweepX[0] # is not 0 for 'wait for trigger' FV3000
	firstSampleTime = ba1.abf.tagTimesSec[0]
	print('firstSampleTime:', firstSampleTime)

	# for each spike in e-phys, match it with a spike in imaging
	# e-phys is shorter, fewer spikes
	numSpikes = ba1.numSpikes
	print('num spikes in recording:', numSpikes)

	thresholdSec0, peakSec0 = ba0.getStat('thresholdSec', 'peakSec')
	thresholdSec1, peakSec1 = ba1.getStat('thresholdSec', 'peakSec')

	ba1_width50, throwOut = ba1.getStat('widths_50', 'peakSec')

	# todo: add an option in bAnalysis.getStat()
	thresholdSec0 = [x + firstSampleTime for x in thresholdSec0]
	peakSec0 = [x + firstSampleTime for x in peakSec0]

	# assuming spike-detection is clean
	# truncate imaging (it is longer than e-phys)
	thresholdSec0 = thresholdSec0[0:numSpikes] # second value/max is NOT INCLUSIVE
	peakSec0 = peakSec0[0:numSpikes]

	numSubplots = 2
	fig, axs = plt.subplots(numSubplots, 1, sharex=False)

	# threshold in image starts about 20 ms after Vm
	axs[0].plot(thresholdSec1, peakSec0, 'ok')
	#axs[0].plot(thresholdSec1, 'ok')

	# draw diagonal
	axs[0].plot([0, 1], [0, 1], transform=axs[0].transAxes)

	axs[0].set_xlabel('thresholdSec1')
	axs[0].set_ylabel('peakSec0')

	#axs[1].plot(thresholdSec1, peakSec0, 'ok')

	# time to peak in image wrt AP threshold time
	caTimeToPeak = []
	for idx, thresholdSec in enumerate(thresholdSec1):
		timeToPeak = peakSec0[idx] - thresholdSec
		#print('thresholdSec:', thresholdSec, 'peakSec0:', peakSec0[idx], 'timeToPeak:', timeToPeak)
		caTimeToPeak.append(timeToPeak)

	print('caTimeToPeak:', caTimeToPeak)

	axs[1].plot(ba1_width50, caTimeToPeak, 'ok')

	# draw diagonal
	#axs[1].plot([0, 1], [0, 1], transform=axs[1].transAxes)

	axs[1].set_xlabel('ba1_width50')
	axs[1].set_ylabel('caTimeToPeak')

	#
	plt.show()

def test_load_abf():
	path = 'data/19114001.abf' # needs to be run fron SanPy
	print('=== test_load_abf() path:', path)
	ba = bAnalysis(path)

	dDict = sanpy.bAnalysis.getDefaultDetection()
	ba.spikeDetect(dDict)

	print('  ba.numSpikes:', ba.numSpikes)
	ba.openHeaderInBrowser()

def test_load_csv():
	path = 'data/19114001.csv' # needs to be run fron SanPy
	print('=== test_load_csv() path:', path)
	ba = bAnalysis(path)

	dDict = sanpy.bAnalysis.getDefaultDetection()
	ba.spikeDetect(dDict)

	print('  ba.numSpikes:', ba.numSpikes)

def test_save():
	path = 'data/19114001.abf' # needs to be run fron SanPy
	ba = bAnalysis(path)

	dDict = sanpy.bAnalysis.getDefaultDetection()
	ba.spikeDetect(dDict)

	#ba.save_csv()

def main():
	import matplotlib.pyplot as plt

	test_load_abf()
	test_load_csv()

	sys.exit(1)

	test_save()

	'''
	if 0:
		print('running bAnalysis __main__')
		ba = bAnalysis('../data/19114001.abf')
		print(ba.dataPointsPerMs)
	'''

	# this is to load/analyze/plot the sum of a number of Ca imaging line scans
	# e.g. lcr
	if 0:
		ba0 = test_load_tif(path) # this can load a line scan tif
		# todo: add title
		test_plot(ba0)

		ba1 = test_load_abf()
		test_plot(ba1)

		#
		plt.show()

	if 0:
		lcrDualAnalysis()

	if 0:
		path = '/Users/cudmore/Sites/SanPy/examples/dual-analysis/dual-data/20210129/2021_01_29_0007.abf'
		ba = bAnalysis(path)
		dDict = sanpy.bAnalysis.getDefaultDetection()
		#dDict['dvdtThreshold'] = None # detect using just Vm
		print('dDict:', dDict)
		ba.spikeDetect(dDict)
		ba.errorReport()

	if 1:
		path = 'data/19114001.abf'
		ba = bAnalysis(path)
		dDict = sanpy.bAnalysis.getDefaultDetection()
		#dDict['dvdtThreshold'] = None # detect using just Vm

		recordingFrequency = ba.recordingFrequency
		print('recordingFrequency:', recordingFrequency)

		ba.spikeDetect(dDict)
		ba.errorReport()

		headerDict = ba.api_getHeader()
		print('--- headerDictheaderDict')
		for k,v in headerDict.items():
			print('  ', k, ':' ,v)

		if 0:
			oneSpike = 1
			oneSpikeList = ba.api_getSpikeInfo(oneSpike)
			for idx, spikeDict in enumerate(oneSpikeList):
				print('--- oneSpikeList for oneSpike:', oneSpike, 'idx:', idx)
				for k,v in spikeDict.items():
					print('  ', k,v)

		stat = 'peakSec'
		statList = ba.api_getSpikeStat(stat)
		print('--- stat:', stat)
		print(statList)

		recDict = ba.api_getRecording()
		print('--- recDict')
		for k,v in recDict.items():
			print('  ', k, 'len:', len(v), np.nanmean(v))

def test_hdf():
	path = '/home/cudmore/Sites/SanPy/data/19114001.abf'
	ba = sanpy.bAnalysis(path)
	ba.spikeDetect()

def test_sweeps():
	path = '/home/cudmore/Sites/SanPy/data/tests/171116sh_0018.abf'
	ba = sanpy.bAnalysis(path)
	logger.info(ba.numSweeps)
	logger.info(ba.sweepList)

	# plot all sweeps
	import matplotlib.pyplot as plt
	numSpikes = []
	for sweepNumber in ba.abf.sweepList:
		sweepSet = ba.setSweep(sweepNumber)
		ba.spikeDetect()
		print(f'   sweepNumber:{sweepNumber} numSpikes:{ba.numSpikes} maxY:{np.nanmax(ba.sweepY)}')
		numSpikes.append(ba.numSpikes)
		offset = 0 #140*sweepNumber
		print(f'   {type(ba.abf.sweepX)}, {ba.abf.sweepX.shape}')
		plt.plot(ba.abf.sweepX, ba.abf.sweepY+offset, color='C0')
	print(f'numSpikes:{numSpikes}')
	#plt.gca().get_yaxis().set_visible(False)  # hide Y axis
	plt.xlabel(ba.abf.sweepLabelX)
	plt.show()

if __name__ == '__main__':
	# was using this for manuscript
	#main()
	#test_hdf()

	test_sweeps()
