"""
Configuration loader for the Production Sequencing Optimizer.

Everything the pipeline needs at runtime is read from config/config.yaml via
this module. No tunable value is hardcoded anywhere else in src/psopt/.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "config.yaml",
)


@dataclass
class CatalogConfig:
    catalog_name: str
    schema_name: str
    tables: dict

    @property
    def full_schema(self) -> str:
        return f"{self.catalog_name}.{self.schema_name}"

    def table_fqn(self, key: str) -> str:
        """Fully-qualified `catalog.schema.table` name for a logical table key."""
        return f"{self.full_schema}.{self.tables[key]}"


@dataclass
class ObjectiveWeights:
    changeover_hours: float
    idle_hours: float
    unmet_demand_penalty: float
    late_delivery_penalty: float = 0.0  # optional key; defaults to 0.0 (old behavior) if a config.yaml predating this field is used


@dataclass
class ServiceLevel:
    min_total_fulfillment_pct: float
    min_on_time_fulfillment_pct: float
    enforce_as_hard_constraint: bool = False
    forbid_late_delivery: bool = True


@dataclass
class SequencingConfig:
    incompatible_changeover_threshold_hours: Optional[float]
    startup_changeover_minutes: int


@dataclass
class SolverConfig:
    max_time_in_seconds: float
    num_search_workers: int
    log_search_progress: bool
    relative_gap_limit: float


@dataclass
class LotSizingConfig:
    enforce_min_lot_on_partial_fulfillment: bool


@dataclass
class FormatsConfig:
    po_deadline_date_format: str
    calendar_date_format: str
    calendar_time_format: str


@dataclass
class AppConfig:
    catalog: CatalogConfig
    data_backend: str
    local_csv_dir: str
    objective_weights: ObjectiveWeights
    service_level: ServiceLevel
    sequencing: SequencingConfig
    solver: SolverConfig
    lot_sizing: LotSizingConfig
    formats: FormatsConfig
    config_path: str = field(default="")


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load and validate config/config.yaml (or a path override) into an AppConfig."""
    path = config_path or os.environ.get("PSOPT_CONFIG_PATH") or _DEFAULT_CONFIG_PATH
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    cfg = AppConfig(
        catalog=CatalogConfig(**raw["catalog"]),
        data_backend=raw["data_backend"],
        local_csv_dir=raw["local_csv_dir"],
        objective_weights=ObjectiveWeights(**raw["objective_weights"]),
        service_level=ServiceLevel(**raw["service_level"]),
        sequencing=SequencingConfig(**raw["sequencing"]),
        solver=SolverConfig(**raw["solver"]),
        lot_sizing=LotSizingConfig(**raw["lot_sizing"]),
        formats=FormatsConfig(**raw["formats"]),
        config_path=path,
    )

    if cfg.data_backend not in ("unity_catalog", "csv"):
        raise ValueError(
            f"data_backend must be 'unity_catalog' or 'csv', got {cfg.data_backend!r}"
        )
    return cfg
