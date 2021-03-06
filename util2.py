import numpy
from numpy import *
import cPickle as pickle
import zipfile
import shutil

import scipy.stats as stat
from scipy.stats import beta, binom
from scipy.special import gammaln
from math import exp, log

import csv

from data import Datum

from tssb import *

def log_factorial(n):
	return gammaln(n + 1)

def log_bin_coeff(n, k):
	return log_factorial(n) - log_factorial(k) - log_factorial(n - k)

def log_binomial_likelihood(x, n, mu):
	return x * log(mu) + (n - x) * log(1 - mu)

def log_beta(a, b):
	return gammaln(a) + gammaln(b) - gammaln(a + b)

def logsumexp(X, axis=None):
    maxes = numpy.max(X, axis=axis)
    return numpy.log(numpy.sum(numpy.exp(X - maxes), axis=axis)) + maxes

def load_data(fname1,fname2):
	# load ssm data
	reader = csv.DictReader(open(fname1,'rU'), delimiter='\t')
	data = dict()  
	for row in reader:
		name = row['gene'] 
		id = row['id']
		a = [int(x) for x in row['a'].split(',')]
		d = [int(x) for x in row['d'].split(',')]

		mu_r=mu_v=0
		if 'mu_r' in row.keys():
			mu_r = float(row['mu_r'])
			mu_v = float(row['mu_v'])

		data[id] = Datum(name, id, a, d, mu_r, mu_v)
	
	n_ssms = len(data.keys())
	n_cnvs = 0
	
	# load cnv data
	try:
		reader = csv.DictReader(open(fname2,'rU'), delimiter='\t')
		
		
		for row in reader:
			name=row['cnv'] 
			id = row['cnv'] 
			a = [int(x) for x in row['a'].split(',')]
			d = [int(x) for x in row['d'].split(',')]
		
			data[id] = Datum(name, id, a, d,0.999,0.5)
				
			ssms = row['ssms']
			if ssms is None: continue
			if len(ssms)>0:
				for ssm in ssms.split(';'):
					tok = ssm.split(',')
					data[tok[0]].cnv.append((data[id],int(tok[1]),int(tok[2])))
			
		n_cnvs = len(data.keys())-n_ssms

	except Exception as e:
		pass
		
	return [data[key] for key in data.keys()], n_ssms, n_cnvs
	
#################################################
## some useful functions to get some info about,
## the tree, used by CNV related computations
def set_node_height(tssb):
	tssb.root['node'].ht=0
	def descend(root,ht):
		for child in root.children():
			child.ht=ht
			descend(child,ht+1)
	descend(tssb.root['node'],1)
	
def set_path_from_root_to_node(tssb):
	wts, nodes = tssb.get_mixture()
	for node in nodes: node.path = node.get_ancestors()

def map_datum_to_node(tssb):
	wts, nodes = tssb.get_mixture()
	for node in nodes:
		for datum in node.get_data():
			datum.node=node
#################################################

def check_bounds(p,l=0.0001,u=.9999):
	if p < l: p=l
	if p > u: p=u
	return p

# removes the empty nodes from the tssb tree
# Does not removes root as it is not required
# root: root of the current tree
# parent: parent of the root
def remove_empty_nodes(root, parent = None):
	for child in list(root['children']):
		remove_empty_nodes(child, root)
	if (root['node'].get_data() == []):
		if (root['children'] == []): # leaf
			if (parent != None):
				parent['children'].remove(root)
				root['node'].kill()
			return
		else:
			if (parent != None):
				parent_ = root['node'].parent()
				for child in list(root['children']):
					parent['children'].append(child)
					root['children'].remove(child)
				for child in list(root['node'].children()):
					child._parent = parent_
					parent_.add_child(child)
					root['node'].remove_child(child)
				parent['children'].remove(root)
				root['node'].kill()

def rm_safely(filename):
	try:
	    os.remove(filename)
	except OSError as e:
	    if e.errno == 2: # Ignore "no such file" errors
		pass
	    else:
		raise e

class CorruptZipFileError(Exception):
    pass

class BackupManager(object):
    def __init__(self, filenames):
	self._filenames = filenames
	self._backup_filenames = [os.path.realpath(fn) + '.backup' for fn in self._filenames]

    def save_backup(self):
	for fn, backup_fn in zip(self._filenames, self._backup_filenames):
	    shutil.copy2(fn, backup_fn)

    def restore_backup(self):
	for fn, backup_fn in zip(self._filenames, self._backup_filenames):
	    shutil.copy2(backup_fn, fn)

class StateManager(object):
    default_last_state_fn = 'state.last.pickle'
    default_initial_state_fn = 'state.initial.pickle'

    def __init__(self):
	self._initial_state_fn = StateManager.default_initial_state_fn
	self._last_state_fn = StateManager.default_last_state_fn

    def _write_state(self, state, state_fn):
	with open(state_fn, 'w') as state_file:
	    pickle.dump(state, state_file, protocol=pickle.HIGHEST_PROTOCOL)

    def write_state(self, state):
	self._write_state(state, self._last_state_fn)

    def load_state(self):
	with open(self._last_state_fn) as state_file:
	    return pickle.load(state_file)

    def write_initial_state(self, state):
	self._write_state(state, self._initial_state_fn)

    def delete_state_file(self):
	rm_safely(self._last_state_fn)

    def state_exists(self):
	return os.path.isfile(self._last_state_fn)


class TreeWriter(object):
    default_archive_fn = 'trees.zip'

    def __init__(self, resume_run = False):
	self._archive_fn = TreeWriter.default_archive_fn
	if resume_run:
	    self._ensure_archive_is_valid()
	else:
	    # Remove file to avoid unwanted behaviour. By the zipfile module's
	    # behaviour, given that we open the file with the "a" flag, if a
	    # non-zip file exists at this path, a zip file will be appended to
	    # the file; otherwise, if the file is already a zip, additional
	    # files will be written into the zip. On a new run, neither case is
	    # something we want.
	    rm_safely(self._archive_fn)

    def _ensure_archive_is_valid(self):
	with zipfile.ZipFile(self._archive_fn) as zipf:
	    if zipf.testzip() is not None:
		raise CorruptZipFileError('Corrupt zip file: %s' % self._archive_fn)

    def _open_archive(self):
	self._archive = zipfile.ZipFile(self._archive_fn, 'a', compression=zipfile.ZIP_DEFLATED, allowZip64=True)

    def _close_archive(self):
	self._archive.close()

    def _write_tree(self, tree, tree_fn):
	serialized = pickle.dumps(tree, protocol=pickle.HIGHEST_PROTOCOL)
	self._open_archive()
	self._archive.writestr(tree_fn, serialized)
	self._close_archive()

    def write_tree(self, tree, llh, idx):
	self._write_tree(tree, 'tree_%s_%s' % (idx, llh))

    def write_burnin_tree(self, burnin_tree, idx):
	self._write_tree(burnin_tree, 'burnin_%s' % idx)

class TreeReader(object):
    def __init__(self, archive_fn):
	self._archive = zipfile.ZipFile(archive_fn)
	infolist = self._archive.infolist()
	tree_info = [t for t in infolist if t.filename.startswith('tree_')]

	# Sort by index
	tree_info.sort(key = lambda tinfo: self._extract_metadata(tinfo)[0])
	self._trees = []
	for info in tree_info:
	    idx, llh = self._extract_metadata(info)
	    assert idx == len(self._trees)
	    self._trees.append((idx, llh, info))

    def num_trees(self):
	return len(self._trees)

    def close(self):
	self._archive.close()

    def _extract_metadata(self, zinfo):
	tokens = zinfo.filename.split('_')
	idx = int(tokens[1])
	llh = float(tokens[2])
	return (idx, llh)

    def load_tree(self, idx):
	tidx, llh, zinfo = self._trees[idx]
	assert tidx == idx
	pickled = self._archive.read(zinfo)
	return pickle.loads(pickled)

    def load_trees(self, num_trees=None, remove_empty_vertices=False):
	for idx, llh, tree in self.load_trees_and_metadata(num_trees, remove_empty_vertices):
	    yield tree

    def load_trees_and_metadata(self, num_trees=None, remove_empty_vertices=False):
	# Sort by LLH
	trees = sorted(self._trees, key = lambda (tidx, llh, zinfo): llh, reverse=True)

	if num_trees is not None:
	    num_trees = min(num_trees, len(trees))
	    trees = trees[:num_trees]

	for tidx, llh, zinfo in trees:
	    pickled = self._archive.read(zinfo)
	    tree = pickle.loads(pickled)
	    if remove_empty_vertices:
		remove_empty_nodes(tree.root)
	    yield (tidx, llh, tree)
