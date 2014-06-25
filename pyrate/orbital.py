'''
Module for calculating orbital correction for interferograms.

Created on 31/3/13
@author: Ben Davies, NCI
'''

from numpy import empty, isnan, reshape, float32, squeeze
from numpy import dot, vstack, zeros, median, meshgrid
from scipy.linalg import lstsq
from numpy.linalg import pinv

from algorithm import master_slave_ids, get_all_epochs, get_epoch_count


# Orbital correction tasks
#
# TODO: options for multilooking
# 1) do the 2nd stage mlook at prepifg.py/generate up front, then delete in 
#    workflow afterward
# 2) refactor prep_ifgs() call to take input filenames and params & generate
#    mlooked versions from that
#    this needs to be more generic to call at any point in the runtime.


# Design notes:
# The orbital correction code is based on MATLAB Pirate, but includes several
# enhancements. Pirate creates sparse arrays for the linear inversion, which
# contain many empty cells. This is unnecessary for the independent method, and
# temporarily wastes potentially a lot of memory.
#
# For the independent method, PyRate makes individual small design matrices and
# corrects the Ifgs one by one. If required in the correction, the offsets
# option adds an extra colum of ones to include in the inversion.
#
# Network method design matrices are mostly empty, and offsets are handled
# differently. Individual design matrices (== independent method DMs) are
# placed in the sparse network design matrix. Offsets are not included in the
# smaller DMs to prevent unwanted cols being inserted. This is why some funcs
# appear to ignore the offset parameter in the networked method. Network DM
# offsets are cols of 1s in a diagonal line on the LHS of the sparse array.

# constants
INDEPENDENT_METHOD = 'INDEPENDENT_METHOD'
NETWORK_METHOD = 'NETWORK_METHOD'

PLANAR = 'PLANAR'
QUADRATIC = 'QUADRATIC'
PART_CUBIC = 'PART_CUBIC'


def orbital_correction(ifgs, degree, method, mlooked=None, offset=True):
	'''
	Removes orbital error from given Ifgs.

	NB: the ifg data is modified in situ, rather than create intermediate files.
	The network method assumes the given ifgs have already been reduced to a 
	minimum set from an MST type operation.

	ifgs: sequence of Ifg objs to correct
	degree: PLANAR, QUADRATIC or PART_CUBIC
	method: INDEPENDENT_METHOD or NETWORK_METHOD
	mlooked: sequence of multilooked ifgs (must correspond to 'ifgs' arg)
	offset: True/False to include the constant/offset component
	'''
	if degree not in [PLANAR, QUADRATIC, PART_CUBIC]:
		msg = "Invalid degree of %s for orbital correction" % degree
		raise OrbitalError(msg)

	if method == NETWORK_METHOD:
		if mlooked is None:
			_network_correction(ifgs, degree, offset)
		else:
			_validate_mlooked(mlooked, ifgs)
			_network_correction(ifgs, degree, offset, mlooked)

	elif method == INDEPENDENT_METHOD:
		for i in ifgs:
			_independent_correction(i, degree, offset)
	else:
		msg = "Unknown method: '%s', need INDEPENDENT or NETWORK method"
		raise OrbitalError(msg % method)


def _validate_mlooked(mlooked, ifgs):
	'''Basic sanity checking of the multilooked ifgs.'''

	if len(mlooked) != len(ifgs):
		msg = "Mismatching # ifgs and # multilooked ifgs"
		raise OrbitalError(msg)

	tmp = [hasattr(i, 'phase_data') for i in mlooked]
	if all(tmp) is False:
		msg = "Mismatching types in multilooked ifgs arg:\n%s" % mlooked
		raise OrbitalError(msg)


def get_num_params(degree, offset=None):
	'''Returns number of model parameters'''
	if degree == PLANAR:
		nparams = 2
	elif degree == QUADRATIC:
		nparams = 5
	elif degree == PART_CUBIC:
		nparams = 6
	else:
		msg = "Invalid orbital model degree: %s" % degree
		raise OrbitalError(msg)

	# NB: independent method only, network method handles offsets separately 
	if offset is True:
		nparams += 1  # eg. y = mx + offset
	return nparams


def _independent_correction(ifg, degree, offset):
	'''
	Calculates and removes orbital correction from an ifg.

	NB: Changes are made in place to the Ifg obj.
	ifg: the ifg to remove remove the orbital error from
	degree: type of model to use PLANAR, QUADRATIC etc
	offset: 
	'''
	vphase = reshape(ifg.phase_data, ifg.num_cells) # vectorise, keeping NODATA
	dm = get_design_matrix(ifg, degree, offset)

	# filter NaNs out before getting model
	tmp = dm[~isnan(vphase)]
	fd = vphase[~isnan(vphase)]
	model = lstsq(tmp, fd)[0] # first arg is the model params

	# calculate forward model & morph back to 2D
	correction = reshape(dot(dm, model), ifg.phase_data.shape)
	ifg.phase_data -= correction


def _network_correction(ifgs, degree, offset, m_ifgs=None):
	'''
	Calculates orbital correction model, removing this from the ifgs.
	NB: This does in-situ modification of phase_data in the ifgs.

	ifgs - interferograms reduced to a minimum tree from prior MST calculations
	degree - PLANAR, QUADRATIC or PART_CUBIC
	offset - True to calculate the model using offsets
	m_ifgs - multilooked ifgs (sequence must be mlooked versions of 'ifgs' arg)
	'''
	# get DM & filter out NaNs
	src_ifgs = ifgs if m_ifgs is None else m_ifgs
	vphase = vstack([i.phase_data.reshape((i.num_cells, 1)) for i in src_ifgs])
	vphase = squeeze(vphase)
	dm = get_network_design_matrix(src_ifgs, degree, offset)

	# filter NaNs out before getting model
	tmp = dm[~isnan(vphase)]
	fd = vphase[~isnan(vphase)]
	model = dot(pinv(tmp, 1e-6), fd)

	ncoef = get_num_params(degree)
	ids = master_slave_ids(get_all_epochs(ifgs))
	coefs = [model[i:i+ncoef] for i in range(0, len(set(ids)) * ncoef, ncoef)]

	# create DM to expand into surface from params
	dm = get_design_matrix(ifgs[0], degree, offset=False)

	for i in ifgs:
		orb = dm.dot(coefs[ids[i.slave]] - coefs[ids[i.master]])
		orb = orb.reshape(ifgs[0].shape)

		# estimate offsets
		if offset:
			tmp = i.phase_data - orb
			i.phase_data -= (orb + median(tmp[~isnan(tmp)]))
		else:
			i.phase_data -= orb


def get_design_matrix(ifg, degree, offset):
	'''
	Returns simple design matrix with columns for model parameters.
	ifg - interferogram to base the DM on
	degree - PLANAR, QUADRATIC or PART_CUBIC
	offset - True to include offset cols, otherwise False.
	'''
	if degree not in [PLANAR, QUADRATIC, PART_CUBIC]:
		raise OrbitalError("Invalid degree argument")

	# apply positional parameter values, multiply pixel coordinate by cell size to
	# get distance (a coord by itself doesn't tell us distance from origin)

	# init design matrix
	data = empty((ifg.num_cells, get_num_params(degree, offset)), dtype=float32)
	x, y = meshgrid(range(ifg.ncols), range(ifg.nrows))
	# TODO: subtract reference pixel coordinate from x and y
	# TODO? divide x and y by 100km, this would increase the size of the estimated params. 
	# test whether this improves robustness of numerical inversion
	x = x.reshape(ifg.num_cells) * ifg.x_size
	y = y.reshape(ifg.num_cells) * ifg.y_size

	# TODO: performance test this vs np.concatenate (n by 1 cols)

	if degree == PLANAR:
		data[:, 0] = x
		data[:, 1] = y
	elif degree == QUADRATIC:
		data[:, 0] = x**2
		data[:, 1] = y**2
		data[:, 2] = x * y
		data[:, 3] = x
		data[:, 4] = y
	elif degree == PART_CUBIC:
		data[:, 0] = x * (y**2)
		data[:, 1] = x**2
		data[:, 2] = y**2
		data[:, 3] = x * y
		data[:, 4] = x
		data[:, 5] = y
	if offset is True:
		data[:, -1] = 1

	return data


def get_network_design_matrix(ifgs, degree, offset):
	'''
	Returns larger format design matrix for networked error correction.

	The network design matrix includes rows which relate to those of NaN cells.
	ifgs - sequence of interferograms
	degree - PLANAR, QUADRATIC or PART_CUBIC
	offset - True to include offset cols, otherwise False.
	'''
	if degree not in [PLANAR, QUADRATIC, PART_CUBIC]:
		raise OrbitalError("Invalid degree argument")

	nifgs = len(ifgs)
	if nifgs < 1:
		# can feasibly do correction on a single Ifg/2 epochs
		raise OrbitalError("Invalid number of Ifgs: %s" % nifgs)

	# init sparse network design matrix
	nepochs = get_epoch_count(ifgs)
	ncoef = get_num_params(degree) # no offsets: they are made separately below
	shape = [ifgs[0].num_cells * nifgs, ncoef * nepochs]
	if offset:
		shape[1] += nifgs # add extra block for offset cols

	ndm = zeros(shape, dtype=float32)

	# calc location for individual design matrices
	dates = [ifg.master for ifg in ifgs] + [ifg.slave for ifg in ifgs]
	ids = master_slave_ids(dates)
	offset_col = nepochs * ncoef # base offset for the offset cols
	tmp = get_design_matrix(ifgs[0], degree, offset=False)

	# iteratively build up sparse matrix
	for i, ifg in enumerate(ifgs):
		rs = i * ifg.num_cells # starting row
		m = ids[ifg.master] * ncoef  # start col for master
		s = ids[ifg.slave] * ncoef  # start col for slave
		ndm[rs:rs + ifg.num_cells, m:m + ncoef] = -tmp
		ndm[rs:rs + ifg.num_cells, s:s + ncoef] = tmp

		# offsets are diagonal cols across the extra array block created above
		if offset:
			ndm[rs:rs + ifg.num_cells, offset_col + i] = 1  # init offset cols

	return ndm


class OrbitalError(Exception):
	'''Generic class for errors in orbital correction'''
	pass
