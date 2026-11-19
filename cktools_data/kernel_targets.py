# Helper class to provide a list of kernel and kernel-related packages and projects
# This is primarily used for LP bug management

from dataclasses import dataclass, field

from .kernel_series import KernelSeries

# List of non-kernel packages we care about
PackagesOfInterest = [
    "bluez-firmware",
    "linux-base",
    "linux-firmware",
    "linux-firmware-amd-graphics",
    "linux-firmware-amd-misc",
    "linux-firmware-broadcom-wireless",
    "linux-firmware-intel-graphics",
    "linux-firmware-intel-misc",
    "linux-firmware-intel-wireless",
    "linux-firmware-marvell-prestera",
    "linux-firmware-marvell-wireless",
    "linux-firmware-mediatek",
    "linux-firmware-mellanox-spectrum",
    "linux-firmware-misc",
    "linux-firmware-netronome",
    "linux-firmware-nvidia-graphics",
    "linux-firmware-qlogic",
    "linux-firmware-qualcomm-graphics",
    "linux-firmware-qualcomm-misc",
    "linux-firmware-qualcomm-wireless",
    "linux-firmware-realtek",
    "lowlatency-kernel",
    "ubuntu-realtime",
]


# List of kernel-related projects we care about
ProjectsOfInterest = [
    "canonical-kernel-snaps",
    "ubuntu-realtime",
]


# frozen=True is required to use KernelTarget with set()
@dataclass(frozen=True)
class KernelTarget:
    name: str
    type: str
    series: str = None
    supported: bool = True
    devel: bool = True


@dataclass
class KernelTargets:
    packages: list = field(default_factory=list)
    projects: list = field(default_factory=list)

    # The below are overwritten for unit testing
    _ks: KernelSeries = None

    def __post_init__(self):
        self._ks = self._ks or KernelSeries()
        packages = set()
        projects = set()

        # Add all kernel source packages from kernel-series
        for series in self._ks.series:
            for source in series.sources:
                for package in source.packages:
                    packages.add(
                        KernelTarget(
                            name=package.name,
                            type="package",
                            series=series.codename,
                            supported=(series.supported and source.supported),
                            devel=series.development,
                        )
                    )

        # Add the hard-coded packages
        for pkg in PackagesOfInterest:
            packages.add(
                KernelTarget(
                    name=pkg,
                    type="package",
                )
            )

        # Add the hard-coded projects
        for prj in ProjectsOfInterest:
            projects.add(
                KernelTarget(
                    name=prj,
                    type="project",
                )
            )

        self.packages = list(packages)
        self.projects = list(projects)

    @property
    def all(self):
        return self.packages + self.projects


if __name__ == "__main__":
    kt = KernelTargets()
    print("II: Packages")
    for p in kt.packages:
        print(p)
    print("II: Projects")
    for p in kt.projects:
        print(p)
