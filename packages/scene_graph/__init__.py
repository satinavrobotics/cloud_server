"""Free-space scene graph: `api.py` is the contract, `engine.ChartGraph` the engine, `store.SceneGraphStore` the persistence.
Only the exceptions are re-exported here, so a service can catch them by type without importing the engine."""
from .api import EngineFaulted, LeafError, MissingTiles
from .store import DirtyCoverageError, StaleVersion, StateMismatch, TooOld

SaveFenced = StaleVersion        # a writer that lost its lock or its compare-and-set

__all__ = ["MissingTiles", "LeafError", "EngineFaulted", "StaleVersion", "SaveFenced", "TooOld", "StateMismatch",
           "DirtyCoverageError"]
