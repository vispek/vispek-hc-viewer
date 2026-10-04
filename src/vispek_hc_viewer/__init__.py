# SPDX-License-Identifier: Apache-2.0
"""A local web viewer for the Vispek HC1500: live view, LED control, scans, spectra.

Start it with ``vispek-hc-viewer`` (or ``python -m vispek_hc_viewer``). It serves one page
on 127.0.0.1 and talks to the device through the ``vispek_hc`` package.
"""

from importlib.metadata import version

__version__ = version("vispek-hc-viewer")
