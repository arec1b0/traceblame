# Upstream alert rules (vendored, unchanged)

`kubestate-exporter.yml` and `blackbox-exporter.yml` are `dist/rules/kubernetes/kubestate-exporter.yml`
and `dist/rules/blackbox/blackbox-exporter.yml` of
[samber/awesome-prometheus-alerts](https://github.com/samber/awesome-prometheus-alerts) at commit
`736370e922cee74db497391c825a7a6bd3d48f17`, licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) by its authors. Their sha256 is in
`tools/checksums.txt`; pre-commit leaves this directory alone so the bytes stay the upstream ones.

`deploy/kind/runbook/monitoring/rules.yml` takes four of these rules with their expressions and
annotations unchanged; `tests/scripts/test_runbook_rules.py` checks that against these files and
lists every field that differs (`rules.py`).
