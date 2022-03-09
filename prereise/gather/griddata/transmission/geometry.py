import cmath
from dataclasses import dataclass, field
from itertools import combinations
from math import exp, log, pi, sqrt
from statistics import geometric_mean

from prereise.gather.griddata.transmission.const import (
    epsilon_0,
    mu_0,
    relative_permeability,
    resistivity,
)
from prereise.gather.griddata.transmission.helpers import DataclassWithValidation


@dataclass
class Conductor(DataclassWithValidation):
    """Represent a single conductor (which may be a stranded compsite).

    :param float radius: outer radius of conductor.
    :param str material: material of conductor. Used to calculate ``resistance_per_km``
        and ``gmr`` if these aren't passed to the constructor, unnecessary otherwise.
    :param float resistance_per_km: resistance (ohms) per kilometer. Will be estimated
        from other parameters if it isn't passed.
    :param float gmr: geometric mean radius of conductor. Will be estimated from
        other parameters if it isn't passed.
    :param float area: cross-sectional area of conductor. Will be estimated from
        other parameters if it isn't passed.
    """

    radius: float
    material: str = None
    resistance_per_km: float = None
    gmr: float = None
    area: float = None
    permeability: float = None

    def __post_init__(self):
        # Validate inputs
        self.validate_input_types()  # defined in DataclassWithValidation
        if self.gmr is None and (self.material is None):
            raise ValueError(
                "If gmr is not provided, material and radius are needed to estimate"
            )
        if self.resistance_per_km is None and self.material is None:
            raise ValueError(
                "If resistance_per_km is not provided, material is needed to estimate"
            )
        # Estimate missing inputs using the inputs which are present
        if self.gmr is None:
            try:
                self.permeability = relative_permeability[self.material]
            except KeyError:
                raise ValueError(
                    f"Unknown permeability for {self.material}, can't calculate gmr"
                )
            self.gmr = self.radius * exp(self.permeability / 4)

        if self.resistance_per_km is None:
            try:
                self.resistivity = resistivity[self.material]
            except KeyError:
                raise ValueError(
                    f"Unknown resistivity for {self.material}, "
                    "can't calculate resistance"
                )
            if self.area is None:
                self.area = pi * self.radius**2
            # convert per-m to per-km
            self.resistance_per_km = self.resistivity * 1000 / self.area


@dataclass
class ConductorBundle(DataclassWithValidation):
    """Represent a bundle of conductors (or a 'bundle' of one).

    :param int n: number of conductors in bundle (can be one).
    :param Conductor conductor: information for each conductor.
    :param float spacing: distance between the centers of each conductor (meters).
    :param str layout: either 'circular' (conductors are arranged in a regular polygon
        with edge length ``spacing``) or 'flat' (conductors are arranged in a line, at
        regular spacing ``spacing``).
    """

    n: int
    conductor: Conductor
    spacing: float = None  # we need to be able to ignore spacing for a single conductor
    layout: str = "circular"
    resistance_per_km: float = field(init=False)
    spacing_L: float = field(init=False)  # noqa: N815
    spacing_C: float = field(init=False)  # noqa: N815

    def __post_init__(self):
        self.validate_input_types()  # defined in DataclassWithValidation
        self.resistance_per_km = self.conductor.resistance_per_km / self.n
        self.spacing_L = self.calculate_equivalent_spacing("inductance")
        self.spacing_C = self.calculate_equivalent_spacing("capacitance")

    def calculate_equivalent_spacing(self, type="inductance"):
        if type == "inductance":
            conductor_distance = self.conductor.gmr
        elif type == "capacitance":
            conductor_distance = self.conductor.radius
        else:
            raise ValueError("type must be either 'inductance' or 'capacitance'")
        if self.n == 1:
            return conductor_distance
        elif self.n == 2:
            return (conductor_distance * self.spacing) ** (1 / 2)
        else:
            if self.layout == "circular":
                return self.calculate_equivalent_spacing_circular(conductor_distance)
            if self.layout == "flat":
                return self.calculate_equivalent_spacing_flat(conductor_distance)
            raise ValueError(f"Unknown layout: {self.layout}")

    def calculate_equivalent_spacing_circular(self, conductor_distance):
        if self.n == 3:
            return (conductor_distance * self.spacing**2) ** (1 / 3)
        if self.n == 4:
            return (conductor_distance * self.spacing**3 * 2 ** (1 / 2)) ** (1 / 4)
        raise NotImplementedError(
            "Geometry calculations are only implemented for 1 <= n <= 4"
        )

    def calculate_equivalent_spacing_flat(self, conductor_distance):
        if self.n == 3:
            return (conductor_distance * 2 * self.spacing**2) ** (1 / 3)
        if self.n == 4:
            return (conductor_distance * 12 * self.spacing**3) ** (1 / 8)
        raise NotImplementedError(
            "Geometry calculations are only implemented for 1 <= n <= 4"
        )


@dataclass
class PhaseLocations(DataclassWithValidation):
    """Represent the locations of each conductor bundle on a transmission tower. Each of
    ``a``, ``b``, and ``c`` are the (x, y) location(s) of that phase's conductor(s).

    :param tuple a: the (x, y) location of the single 'A' phase conductor if
        ``circuits`` == 1, or the ((x1, y1), (x2, y2), ...) locations of the 'A' phase
        conductors if ``circuits`` > 1. Units are meters.
    :param tuple b: the (x, y) location of the single 'B' phase conductor if
        ``circuits`` == 1, or the ((x1, y1), (x2, y2), ...) locations of the 'B' phase
        conductors if ``circuits`` > 1. Units are meters.
    :param tuple c: the (x, y) location of the single 'C' phase conductor if
        ``circuits`` == 1, or the ((x1, y1), (x2, y2), ...) locations of the 'C' phase
        conductors if ``circuits`` > 1. Units are meters.
    :param int circuits: the number of circuits on the tower.
    """

    a: tuple
    b: tuple
    c: tuple
    circuits: int = 1
    equivalent_distance: float = field(init=False)
    equivalent_height: float = field(init=False)
    phase_self_distances: dict = field(init=False, default=None)
    equivalent_reflected_distance: float = field(init=False)

    def __post_init__(self):
        self.validate_input_types()  # defined in DataclassWithValidation
        if not (len(self.a) == len(self.b) == len(self.c)):
            raise ValueError("each phase location must have the same length")
        if self.circuits == 1 and len(self.a) == 2 and isinstance(self.a[0], float):
            # Single-circuit specified as (x, y) will be converted to ((x, y))
            self.a = (self.a,)
            self.b = (self.b,)
            self.c = (self.c,)
        self.calculate_distances()

    def calculate_distances(self):
        self.true_distance = {
            "ab": _geometric_mean_euclidian(self.a, self.b),
            "ac": _geometric_mean_euclidian(self.a, self.c),
            "bc": _geometric_mean_euclidian(self.b, self.c),
        }
        # 'Equivalent' distances are geometric means
        self.equivalent_distance = geometric_mean(self.true_distance.values())
        self.equivalent_height = geometric_mean(
            [self.a[0][1], self.b[0][1], self.c[0][1]]
        )
        if self.circuits == 1:
            self.calculate_single_circuit_distances()
        else:
            self.calculate_multi_circuit_distances()

    def calculate_single_circuit_distances(self):
        # The distance bounced off the ground, or 'reflected', is used for
        # single-circuit capacitance calculations
        self.reflected_distance = {
            "ab": _euclidian(self.a[0], (self.b[0][0], -self.b[0][1])),  # a -> b'
            "ac": _euclidian(self.a[0], (self.c[0][0], -self.c[0][1])),  # a -> c'
            "bc": _euclidian(self.b[0], (self.c[0][0], -self.c[0][1])),  # b -> c'
        }
        self.equivalent_reflected_distance = geometric_mean(
            self.reflected_distance.values()
        )

    def calculate_multi_circuit_distances(self):
        self.phase_self_distances = [
            geometric_mean(_euclidian(p0, p1) for p0, p1 in combinations(phase, 2))
            for phase in (self.a, self.b, self.c)
        ]
        # Multi circuit, so we assume tall tower negligible impact from reflectance
        self.equivalent_reflected_distance = 2 * self.equivalent_height


@dataclass
class Tower(DataclassWithValidation):
    """Given the geometry of a transmission tower and conductor bundle information,
    estimate per-kilometer inductance, resistance, and shunt capacitance.

    :param PhaseLocations locations: the locations of each conductor bundle.
    :param ConductorBundle bundle: the parameters of each conductor bundle.
    """

    locations: PhaseLocations
    bundle: ConductorBundle
    resistance: float = field(init=False)
    inductance: float = field(init=False)
    capacitance: float = field(init=False)

    def __post_init__(self):
        self.validate_input_types()  # defined in DataclassWithValidation
        self.resistance = self.bundle.resistance_per_km / self.locations.circuits
        self.inductance = self.calculate_inductance_per_km()
        self.capacitance = self.calculate_shunt_capacitance_per_km()

    def calculate_inductance_per_km(self):
        denominator = _circuit_bundle_distances(
            self.bundle.spacing_L, self.locations.phase_self_distances
        )
        inductance_per_km = (
            mu_0 / (2 * pi) * log(self.locations.equivalent_distance / denominator)
        )
        return inductance_per_km

    def calculate_shunt_capacitance_per_km(self):
        denominator = _circuit_bundle_distances(
            self.bundle.spacing_C, self.locations.phase_self_distances
        )
        capacitance_per_km = (2 * pi * epsilon_0) / (
            log(self.locations.equivalent_distance / denominator)
            - log(
                self.locations.equivalent_reflected_distance
                / (2 * self.locations.equivalent_height)
            )
        )
        return capacitance_per_km


@dataclass
class Line(DataclassWithValidation):
    """Given a Tower design, line voltage, and length, calculate whole-line impedances
    and rating.

    :param Tower tower: tower parameters (containing per-kilometer impedances).
    :param int/float length: line length (kilometers).
    :param int/float voltage: line voltage (kilovolts).
    :param int/float freq: the system nominal frequency (Hz).
    """

    tower: Tower
    length: float
    voltage: float
    freq: float = 60.0
    series_impedance_per_km: complex = field(init=False)
    shunt_admittance_per_km: complex = field(init=False)
    propogation_constant_per_km: complex = field(init=False)
    surge_impedance: complex = field(init=False)
    series_impedance: complex = field(init=False)
    shunt_admittance: complex = field(init=False)

    def __post_init__(self):
        # Convert integers to floats as necessary
        for attr in ("freq", "length", "voltage"):
            if isinstance(getattr(self, attr), int):
                setattr(self, attr, float(getattr(self, attr)))
        self.validate_input_types()  # defined in DataclassWithValidation
        # Calculate second-order electrical parameters which depend on frequency
        omega = 2 * pi * self.freq
        self.series_impedance_per_km = (
            self.tower.resistance + 1j * self.tower.inductance * omega
        )
        self.shunt_admittance_per_km = 1j * self.tower.capacitance * omega
        self.surge_impedance = cmath.sqrt(
            self.series_impedance_per_km / self.shunt_admittance_per_km
        )
        self.propogation_constant_per_km = cmath.sqrt(
            self.series_impedance_per_km * self.shunt_admittance_per_km
        )
        self.surge_impedance_loading = self.voltage**2 / abs(self.surge_impedance)
        # Use the long-line transmission model to calculate lumped-element parameters
        self.series_impedance = (self.series_impedance_per_km * self.length) * (
            cmath.sinh(self.propogation_constant_per_km * self.length)
            / (self.propogation_constant_per_km * self.length)
        )
        self.shunt_admittance = (self.shunt_admittance_per_km * self.length) * (
            cmath.tanh(self.propogation_constant_per_km * self.length / 2)
            / (self.propogation_constant_per_km * self.length / 2)
        )


def _euclidian(a, b):
    """Calculate the euclidian distance between two points."""
    try:
        if len(a) != len(b):
            raise ValueError("Length of a and b must be equivalent")
    except TypeError:
        raise TypeError(
            "a and b must both be iterables compatible with the len() function"
        )
    return sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _geometric_mean_euclidian(a_list, b_list):
    """Calculate the geometric mean euclidian distance between two coordinate lists."""
    try:
        if len(a_list) != len(b_list):
            raise ValueError("Length of a_list and b_list must be equivalent")
    except TypeError:
        raise TypeError("a_list and b_list must both be iterables")
    return geometric_mean(_euclidian(a, b) for a in a_list for b in b_list)


def _circuit_bundle_distances(bundle_distance, phase_distances=None):
    """Calculate characteristic distance of bundle and circuit distances."""
    if phase_distances is None:
        return bundle_distance
    phase_characteristic_distances = [
        sqrt(phase * bundle_distance) for phase in phase_distances
    ]
    return geometric_mean(phase_characteristic_distances)