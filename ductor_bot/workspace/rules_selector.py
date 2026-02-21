"""Auto-discovery and deployment of provider-specific rule files (CLAUDE.md/AGENTS.md)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.auth import AuthStatus, check_all_auth

if TYPE_CHECKING:
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


class RulesSelector:
    """Selects and deploys rule files based on CLI authentication status.

    Template naming in _home_defaults/:
    - RULES.md (static rules for all providers)
    - RULES-claude-only.md (Claude-specific variant)
    - RULES-codex-only.md (Codex-specific variant)
    - RULES-claude-and-codex.md (both providers variant)

    Deployed naming in ~/.ductor/:
    - CLAUDE.md (always created if Claude authenticated)
    - AGENTS.md (always created if Codex authenticated)
    - Both synchronized via sync_rule_files() when both exist

    Usage:
        selector = RulesSelector(paths)
        selector.deploy_rules()
    """

    def __init__(self, paths: DuctorPaths) -> None:
        self._paths = paths
        # Cache auth status to avoid multiple checks
        auth = check_all_auth()
        claude_result = auth.get("claude")
        codex_result = auth.get("codex")
        gemini_result = auth.get("gemini")

        self._claude_authenticated = (
            claude_result.status == AuthStatus.AUTHENTICATED if claude_result else False
        )
        self._codex_authenticated = (
            codex_result.status == AuthStatus.AUTHENTICATED if codex_result else False
        )
        self._gemini_authenticated = (
            gemini_result.status == AuthStatus.AUTHENTICATED if gemini_result else False
        )

    def get_variant_suffix(self) -> str:
        """Determine template variant based on CLI authentication status.

        Returns:
            "claude-and-codex" if both authenticated
            "codex-only" if only Codex or Gemini authenticated
            "claude-only" otherwise (default fallback)
        """
        # We treat Gemini as an 'Agent' (similar to Codex)
        has_agent = self._codex_authenticated or self._gemini_authenticated

        if self._claude_authenticated and has_agent:
            return "claude-and-codex"
        if has_agent:
            return "codex-only"
        return "claude-only"

    def discover_template_directories(self) -> list[Path]:
        """Find all directories in _home_defaults/ containing RULES templates.

        Returns:
            List of directories containing at least one RULES*.md file.
        """
        seen: set[Path] = set()
        candidates: list[Path] = []

        for path in self._paths.home_defaults.rglob("RULES*.md"):
            parent = path.parent
            if parent not in seen:
                seen.add(parent)
                candidates.append(parent)

        return sorted(candidates)

    def get_best_template(self, directory: Path) -> Path | None:
        """Select best RULES template for directory based on auth status.

        Priority:
        1. Variant-specific template (RULES-{variant}.md)
        2. Static fallback template (RULES.md)

        Args:
            directory: Directory to check for templates

        Returns:
            Path to selected template, or None if no templates found
        """
        variant = self.get_variant_suffix()

        # Priority 1: Variant-specific template
        variant_template = directory / f"RULES-{variant}.md"
        if variant_template.exists():
            logger.debug("Selected variant template: %s", variant_template.name)
            return variant_template

        # Priority 2: Static fallback
        static_template = directory / "RULES.md"
        if static_template.exists():
            logger.debug("Selected static template: %s", static_template.name)
            return static_template

        return None

    def deploy_rules(self) -> None:
        """Auto-discover and deploy all rule files to ~/.ductor/.

        Scans _home_defaults/ for directories with RULES templates, selects
        the best variant for current auth state, and deploys to ~/.ductor/
        as CLAUDE.md and/or AGENTS.md based on authentication status.

        Deployment logic:
        - Only Claude authenticated → only CLAUDE.md
        - Only Codex authenticated → only AGENTS.md
        - Both authenticated → both CLAUDE.md and AGENTS.md (kept in sync)
        """
        variant = self.get_variant_suffix()
        logger.info(
            "Deploying rule files (variant: %s, claude=%s, codex=%s)",
            variant,
            self._claude_authenticated,
            self._codex_authenticated,
        )

        template_dirs = self.discover_template_directories()
        deployed_count = 0

        for template_dir in template_dirs:
            # Calculate relative path from _home_defaults/
            try:
                rel_path = template_dir.relative_to(self._paths.home_defaults)
            except ValueError:
                logger.warning("Template dir outside home_defaults: %s", template_dir)
                continue

            # Find best template for this directory
            template = self.get_best_template(template_dir)
            if not template:
                logger.debug("No templates found in: %s", rel_path)
                continue

            # Deploy based on auth status
            dst_dir = self._paths.ductor_home / rel_path
            dst_dir.mkdir(parents=True, exist_ok=True)

            try:
                # Deploy CLAUDE.md if Claude is authenticated
                if self._claude_authenticated:
                    claude_dst = dst_dir / "CLAUDE.md"
                    shutil.copy2(template, claude_dst)
                    deployed_count += 1
                    logger.debug("Deployed: %s -> CLAUDE.md", template.name)

                # Deploy AGENTS.md if Codex or Gemini is authenticated
                if self._codex_authenticated or self._gemini_authenticated:
                    agents_dst = dst_dir / "AGENTS.md"
                    shutil.copy2(template, agents_dst)
                    deployed_count += 1
                    logger.debug("Deployed: %s -> AGENTS.md", template.name)

                # Deploy GEMINI.md if Gemini is authenticated
                if self._gemini_authenticated:
                    gemini_dst = dst_dir / "GEMINI.md"
                    shutil.copy2(template, gemini_dst)
                    deployed_count += 1
                    logger.debug("Deployed: %s -> GEMINI.md", template.name)

            except OSError:
                logger.exception("Failed to deploy %s", template)

        logger.info(
            "Deployed %d rule files (Claude=%s, Codex=%s, Gemini=%s)",
            deployed_count,
            self._claude_authenticated,
            self._codex_authenticated,
            self._gemini_authenticated,
        )

        # Cleanup: Remove stale files that don't match current auth status
        self._cleanup_stale_files()

    def _cleanup_stale_files(self) -> None:
        """Remove CLAUDE.md or AGENTS.md files that don't match current auth status.

        Cleanup logic:
        - Only Claude authenticated → remove all AGENTS.md files
        - Only Codex/Gemini authenticated → remove all CLAUDE.md files
        - Both authenticated → keep both (they stay in sync via sync_rule_files)
        """
        has_agent = self._codex_authenticated or self._gemini_authenticated

        # If both authenticated, no cleanup needed
        if self._claude_authenticated and has_agent:
            logger.debug("Both CLIs authenticated, no cleanup needed")
            return

        # Only Claude authenticated → remove AGENTS.md
        if self._claude_authenticated and not has_agent:
            removed = self._remove_files_by_name("AGENTS.md")
            if removed > 0:
                logger.info(
                    "Cleaned up %d stale AGENTS.md files (No non-Claude agent authenticated)",
                    removed,
                )
            return

        # Only Codex/Gemini authenticated → remove CLAUDE.md
        if has_agent and not self._claude_authenticated:
            removed = self._remove_files_by_name("CLAUDE.md")
            if removed > 0:
                logger.info(
                    "Cleaned up %d stale CLAUDE.md files (Claude not authenticated)", removed
                )
            # Special case: If only Codex (but not Gemini) is authenticated, remove GEMINI.md
            if not self._gemini_authenticated:
                self._remove_files_by_name("GEMINI.md")
            return

        # Neither authenticated (shouldn't happen, but handle gracefully)
        logger.warning("No providers authenticated, skipping cleanup")

    def _remove_files_by_name(self, filename: str) -> int:
        """Remove all files with given name in ~/.ductor/.

        Skips files inside ``workspace/cron_tasks/`` — those are user-owned
        rule files created per task and must not be deleted on auth-status
        changes.

        Args:
            filename: Name of files to remove (e.g., "CLAUDE.md" or "AGENTS.md")

        Returns:
            Number of files removed
        """
        cron_tasks_path = self._paths.ductor_home / "workspace" / "cron_tasks"
        removed_count = 0
        for file_path in self._paths.ductor_home.rglob(filename):
            if not file_path.is_file():
                continue
            # Protect user-owned cron task rule files
            if file_path.is_relative_to(cron_tasks_path):
                logger.debug("Skipping user-owned cron task file: %s", file_path)
                continue
            try:
                file_path.unlink()
                removed_count += 1
                logger.debug(
                    "Removed stale file: %s", file_path.relative_to(self._paths.ductor_home)
                )
            except OSError:
                logger.exception("Failed to remove stale file: %s", file_path)

        return removed_count
