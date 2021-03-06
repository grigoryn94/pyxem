# -*- coding: utf-8 -*-
# Copyright 2016-2020 The pyXem developers
#
# This file is part of pyXem.
#
# pyXem is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyXem is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyXem.  If not, see <http://www.gnu.org/licenses/>.

import hyperspy.api as hs
from hyperspy.signal import BaseSignal
from hyperspy.signals import Signal2D
from warnings import warn
import numpy as np
from operator import attrgetter

from pyxem.signals import transfer_navigation_axes
from pyxem.signals.diffraction_vectors import generate_marker_inputs_from_peaks
from pyxem.utils.indexation_utils import get_nth_best_solution

from orix.quaternion import Rotation
from orix.crystal_map import CrystalMap

from transforms3d.euler import mat2euler

def crystal_from_vector_matching(z_matches):
    """Takes vector matching results for a single navigation position and
    returns the best matching phase and orientation with correlation and
    reliability to define a crystallographic map.

    Parameters
    ----------
    z_matches : numpy.array
        Template matching results in an array of shape (m,5) sorted by
        total_error (ascending) within each phase, with entries
        [phase, R, match_rate, ehkls, total_error]

    Returns
    -------
    results_array : numpy.array
        Crystallographic mapping results in an array of shape (3) with entries
        [phase, np.array((z, x, z)), dict(metrics)]
    """
    if z_matches.shape == (1,):  # pragma: no cover
        z_matches = z_matches[0]

    # Create empty array for results.
    results_array = np.empty(3, dtype="object")

    # get best matching phase
    best_match = get_nth_best_solution(
        z_matches, "vector", key="total_error", descending=False
    )
    results_array[0] = best_match.phase_index

    # get best matching orientation Euler angles
    results_array[1] = np.rad2deg(mat2euler(best_match.rotation_matrix, "rzxz"))

    # get vector matching metrics
    metrics = dict()
    metrics["match_rate"] = best_match.match_rate
    metrics["ehkls"] = best_match.error_hkls
    metrics["total_error"] = best_match.total_error

    # get second highest correlation phase for phase_reliability (if present)
    other_phase_matches = [
        match for match in z_matches if match.phase_index != best_match.phase_index
    ]

    if other_phase_matches:
        second_best_phase = sorted(
            other_phase_matches, key=attrgetter("total_error"), reverse=False
        )[0]

        metrics["phase_reliability"] = 100 * (
            1 - best_match.total_error / second_best_phase.total_error
        )

        # get second best matching orientation for orientation_reliability
        same_phase_matches = [
            match for match in z_matches if match.phase_index == best_match.phase_index
        ]
        second_match = sorted(
            same_phase_matches, key=attrgetter("total_error"), reverse=False
        )[1]
    else:
        # get second best matching orientation for orientation_reliability
        second_match = get_nth_best_solution(
            z_matches, "vector", rank=1, key="total_error", descending=False
        )

    metrics["orientation_reliability"] = 100 * (
        1 - best_match.total_error / (second_match.total_error or 1.0)
    )

    results_array[2] = metrics

    return results_array


def get_phase_name_and_index(library):
    """Get a dictionary of phase names and its corresponding index value in library.keys().

    Parameters
    ----------
    library : DiffractionLibrary
        Diffraction library containing the phases and rotations

    Returns
    -------
    phase_name_index_dict : Dictionary {str : int}
    typically on the form {'phase_name 1' : 0, 'phase_name 2': 1, ...}
    """

    phase_name_index_dict = dict([(y, x) for x, y in enumerate(list(library.keys()))])
    return phase_name_index_dict


def _peaks_from_best_template(single_match_result, library, rank=0):
    """Takes a TemplateMatchingResults object and return the associated peaks,
    to be used in combination with map().

    Parameters
    ----------
    single_match_result : ndarray
        An entry in a TemplateMatchingResults.
    library : DiffractionLibrary
        Diffraction library containing the phases and rotations.
    rank : int
        Get peaks from nth best orientation (default: 0, best vector match)

    Returns
    -------
    peaks : array
        Coordinates of peaks in the matching results object in calibrated units.
    """
    best_fit = get_nth_best_solution(single_match_result, "template", rank=rank)

    phase_names = list(library.keys())
    phase_index = int(best_fit[0])
    phase = phase_names[phase_index]
    simulation = library.get_library_entry(phase=phase, angle=tuple(best_fit[1]))["Sim"]

    peaks = simulation.coordinates[:, :2]  # cut z
    return peaks

def _get_best_match(z):
    """ Returns the match with the highest score for a given navigation pixel

    Parameters
    ----------
    z : np.array
        array with shape (5,n_matches), the 5 elements are phase, alpha, beta, gamma, score

    Returns
    -------
    z_best : np.array
        array with shape (5,)
    """
    return z[:,np.argmax(z[-1,:])]

class GenericMatchingResults():
    def __init__(self,data):
        self.data = hs.signals.Signal2D(data)

    def to_crystal_map(self):
        """
        Exports an indexation result with multiple results per navigation position to
        crystal map with one result per pixel

        Returns
        -------
        orix.CrystalMap
        """
        _s = self.data.map(_get_best_match,inplace=False)

        """ Gets properties """
        phase_id = _s.isig[0].data.flatten()
        alpha = _s.isig[1].data.flatten()
        beta = _s.isig[2].data.flatten()
        gamma = _s.isig[3].data.flatten()
        score = _s.isig[4].data.flatten()

        """ Gets navigation placements """
        xy = np.indices(_s.data.shape[:2])
        x = xy[1].flatten()
        y = xy[0].flatten()

        """ Tidies up so we can put these things into CrystalMap """
        euler = np.vstack((alpha,beta,gamma)).T
        rotations = Rotation.from_euler(euler,convention="bunge", direction="crystal2lab")
        properties = {"score":score}


        return CrystalMap(
                rotations=rotations,
                phase_id=phase_id,
                x=x,
                y=y,
                prop=properties)



class TemplateMatchingResults(GenericMatchingResults):
    """Template matching results containing the top n best matching crystal
    phase and orientation at each navigation position with associated metrics.

    Examples
    --------
    Saving the signal containing all potential matches at each pixel

    >>> TemplateMatchingResult.data.save("filename")

    Exporting the best matches to a crystal map

    >>> xmap = TemplateMatchingResult.to_crystal_map()
    """

    def plot_best_matching_results_on_signal(
        self, signal, library, permanent_markers=True, *args, **kwargs
    ):
        """Plot the best matching diffraction vectors on a signal.

        Parameters
        ----------
        signal : ElectronDiffraction2D
            The ElectronDiffraction2D signal object on which to plot the peaks.
            This signal must have the same navigation dimensions as the peaks.
        library : DiffractionLibrary
            Diffraction library containing the phases and rotations
        permanent_markers : bool
            Permanently save the peaks as markers on the signal
        *args :
            Arguments passed to signal.plot()
        **kwargs :
            Keyword arguments passed to signal.plot()
        """
        match_peaks = self.map(_peaks_from_best_template, library=library, inplace=False)
        mmx, mmy = generate_marker_inputs_from_peaks(match_peaks)
        signal.plot(*args, **kwargs)
        for mx, my in zip(mmx, mmy):
            m = hs.markers.point(x=mx, y=my, color="red", marker="x")
            signal.add_marker(m, plot_marker=True, permanent=permanent_markers)

class VectorMatchingResults(BaseSignal):
    """Vector matching results containing the top n best matching crystal
    phase and orientation at each navigation position with associated metrics.

    Attributes
    ----------
    vectors : DiffractionVectors
        Diffraction vectors indexed.
    hkls : BaseSignal
        Miller indices associated with each diffraction vector.
    """

    _signal_dimension = 0
    _signal_type = "vector_matching"

    def __init__(self, *args, **kwargs):
        BaseSignal.__init__(self, *args, **kwargs)
        # self.axes_manager.set_signal_dimension(2)
        self.vectors = None
        self.hkls = None

    def to_crystal_map(self):
        """Obtain a crystallographic map specifying the best matching phase and
        orientation at each probe position with corresponding metrics.

        Raises
        -------
        ValueError("Currently under development")
        """

        raise ValueError("Currently under development")

        _s = self.map(
            crystal_from_vector_matching, inplace=False)

        """ Gets phase, the easy bit """
        phase_id = _s.isig[0].data.flatten()

        """ Deals with the properties, hard coded as of v0.13 """
        # need to invert an array of dicts into a dict of arrays
        def _map_to_get_property(prop):
            return d[prop]

        # assume same properties at every point of the signal
        key_list = []
        for key in _s.inav[0,0].isig[2]:
            key_list.append(key)

        properties = {}
        for key in key_list:
            _key_signal = _s.isig[2].map(_map_to_get_property,prop=key,inplace=False)
            properties[key] = _key_signal

        """ Deal with the rotations """
        def _map_for_alpha_beta_gamma(ix):
            return z[ix]

        alpha = _s.isig[1].map(_map_for_alpha_beta_gamma,ix=0,inplace=False)
        beta =  _s.isig[1].map(_map_for_alpha_beta_gamma,ix=1,inplace=False)
        gamma = _s.isig[1].map(_map_for_alpha_beta_gamma,ix=2,inplace=False)

        euler = np.vstack((alpha,beta,gamma)).T
        rotations = Rotation.from_euler(euler,convention="bunge", direction="crystal2lab")

        """ Gets navigation placements """
        xy = np.indices(_s.data.shape[:2])
        x = xy[1].flatten()
        y = xy[0].flatten()

        return CrystalMap(
                rotations=rotations,
                phase_id=phase_id,
                x=x,
                y=y,
                prop=properties)

    def get_indexed_diffraction_vectors(
        self, vectors, overwrite=False, *args, **kwargs
    ):
        """Obtain an indexed diffraction vectors object.

        Parameters
        ----------
        vectors : DiffractionVectors
            A diffraction vectors object to be indexed.

        Returns
        -------
        indexed_vectors : DiffractionVectors
            An indexed diffraction vectors object.
        """
        if overwrite is False:
            if vectors.hkls is not None:
                warn(
                    "The vectors supplied are already associated with hkls set "
                    "overwrite=True to replace these hkls."
                )
            else:
                vectors.hkls = self.hkls

        elif overwrite is True:
            vectors.hkls = self.hkls

        return vectors
