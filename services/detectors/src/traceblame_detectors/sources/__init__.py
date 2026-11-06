"""Where trace views come from: ClickHouse (online) or a frozen snapshot (offline). Both hand
the same rows to view.build_view, so the same trace gives the same view."""
