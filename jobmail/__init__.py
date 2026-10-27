"""Job-application mail analyzer.

Pipeline: ingest plugin -> Message-ID dedupe -> Gemini classify ->
cluster into applications -> job-ad enrich -> CSV/JSON reports.
"""

SCHEMA_VERSION = 2
