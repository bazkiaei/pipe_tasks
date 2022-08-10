#
# LSST Data Management System
# Copyright 2008, 2009, 2010, 2011, 2012, 2022 LSST Corporation.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <http://www.lsstcorp.org/LegalNotices/>.
#

from .makeWarp import MakeWarpTask as NewMakeWarpTask
from .makeWarp import MakeWarpConfig as NewMakeWarpConfig
from deprecated.sphinx import deprecated


__all__ = ["MakeWarpTask", "MakeWarpConfig"]


@deprecated(
    reason="makeCoaddTempExp is deprecated. It will be removed after v25. "
    "Please use lsst.pipe.tasks.makeWarp.MakeWarpTask instead.",
    version="v25.0",
    category=FutureWarning,
)
class MakeWarpTask(NewMakeWarpTask):
    pass


@deprecated(
    reason="makeCoaddTempExp is deprecated. It will be removed after v25. "
    "Please use lsst.pipe.tasks.makeWarp.MakeWarpTask instead.",
    version="v25.0",
    category=FutureWarning,
)
class MakeWarpConfig(NewMakeWarpConfig):
    pass
