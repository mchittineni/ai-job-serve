"""Autonomous job application agent.

Pipeline: scrape -> evaluate (Gemini) -> tailor resume/cover letter grounded in
an Obsidian vault -> apply (email) or draft -> track (SQLite + Obsidian note).
"""
