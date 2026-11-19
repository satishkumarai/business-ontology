"""Governed Business Ontology for Snowflake.

Modules
-------
validation  Pure, dependency-free rules (unit-testable outside Snowflake).
authz       Resolves the *calling user's* domain privileges via role grants.
repo        Thin data-access helpers over ONTOLOGY CORE tables.
api         Stored-procedure handlers for the governed write workflow.
importer    Glossary / ontology import (YAML, JSON, SKOS/RDF Turtle).
exporter    Export of approved, caller-visible concepts (JSON, YAML, Turtle).
drift       Representation validation and drift detection.
"""

__version__ = "1.0.0"
