"""OKF (Open Knowledge Format) v0.1 Conformance Validator.

Validates a directory against Google's Open Knowledge Format specification.
"""

from __future__ import annotations
import os
import re
from pathlib import Path
import yaml

def validate_okf_bundle(bundle_path: str | Path) -> tuple[bool, list[str]]:
    """Validate a directory against the Google OKF v0.1 specification.

    Returns:
        (is_conformant, list_of_errors_or_warnings)
    """
    root = Path(bundle_path)
    if not root.exists() or not root.is_dir():
        return False, [f"Bundle path does not exist or is not a directory: {bundle_path}"]

    issues: list[str] = []
    conformant = True

    # 1. Validate index.md
    index_file = root / "index.md"
    if not index_file.exists():
        # index.md is highly recommended but its absence is tolerated in some variations.
        # However, under strict v0.1 conformance we check it.
        issues.append("Warning: 'index.md' was not found at the root of the bundle.")
    else:
        try:
            content = index_file.read_text(encoding="utf-8")
            if content.startswith("---"):
                issues.append("Error: 'index.md' must NOT contain any YAML frontmatter ( triple-dashes '---' found at start).")
                conformant = False
        except Exception as exc:
            issues.append(f"Error reading 'index.md': {exc}")
            conformant = False

    # 2. Scan and validate concept markdown files (concepts/ directory only)
    concepts_dir = root / "concepts"
    concept_files = list(concepts_dir.glob("**/*.md")) if concepts_dir.exists() else []
    concept_count = 0

    for file in concept_files:
        # Skip reserved files at the root
        rel_path = file.relative_to(root)
        if rel_path.name in ("index.md", "log.md") and len(rel_path.parts) == 1:
            continue

        concept_count += 1
        try:
            content = file.read_text(encoding="utf-8")
            if not content.startswith("---"):
                issues.append(f"Error: Concept file '{rel_path}' lacks valid YAML frontmatter header (must start with '---').")
                conformant = False
                continue

            # Parse frontmatter
            parts = content.split("---", 2)
            if len(parts) < 3:
                issues.append(f"Error: Concept file '{rel_path}' has unclosed YAML frontmatter header.")
                conformant = False
                continue

            frontmatter_raw = parts[1]
            try:
                frontmatter = yaml.safe_load(frontmatter_raw)
            except Exception as yexc:
                issues.append(f"Error parsing YAML frontmatter in '{rel_path}': {yexc}")
                conformant = False
                continue

            if not isinstance(frontmatter, dict):
                issues.append(f"Error: Frontmatter in '{rel_path}' is not a valid key-value mapping.")
                conformant = False
                continue

            # Check for required 'type' field
            if "type" not in frontmatter or not str(frontmatter["type"]).strip():
                issues.append(f"Error: Concept file '{rel_path}' lacks the required non-empty 'type' frontmatter field.")
                conformant = False

        except Exception as exc:
            issues.append(f"Error processing concept file '{rel_path}': {exc}")
            conformant = False

    if concept_count == 0:
        issues.append("Warning: No concept markdown files were found in the bundle.")

    return conformant, issues


if __name__ == "__main__":
    import sys
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    print(f"Validating OKF Bundle at: {target_dir}")
    success, reports = validate_okf_bundle(target_dir)
    print("----------------------------------------")
    for report in reports:
        print(report)
    print("----------------------------------------")
    if success:
        print("🎉 SUCCESS: Bundle conforms to Google OKF v0.1!")
        sys.exit(0)
    else:
        print("❌ FAILED: Conformance check found issues.")
        sys.exit(1)
