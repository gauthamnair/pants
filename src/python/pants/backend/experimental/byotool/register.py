from pants.backend.adhoc import run_system_binary
from pants.backend.adhoc.target_types import SystemBinaryTarget
from pants.backend.byotool import lib
from pants.backend.byotool.lib import ByoTool


def target_types():
    return [
        SystemBinaryTarget,
    ]


def rules():
    return [
        # *ByoTool.rules(),
        *run_system_binary.rules(),
        *lib.rules()
    ]
