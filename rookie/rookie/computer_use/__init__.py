"""Computer Use: la única pieza que toca el VNC y conoce la contraseña del escritorio."""

from .guardias import Guardias, PlazoAgotado, TareaCerrada
from .primitivas import Escritorio, Observacion

__all__ = [
    "Escritorio",
    "Guardias",
    "Observacion",
    "PlazoAgotado",
    "TareaCerrada",
]
