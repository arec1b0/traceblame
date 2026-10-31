// Command loadgen drives and measures bench/gateway-scaling (issue #10).
//
//	loadgen thresholds  per-service p95 of the generated traffic, as an env file
//	loadgen run         steady load to an agent over OTLP/gRPC with basic auth
//	loadgen analyze     account for every span of a run and write the report
//	loadgen render      rewrite a report's Markdown from its JSON (no rerun)
//	loadgen probe       try a TCP connection (NetworkPolicy check)
//
// Traffic replays the content-off golden fixture spans (internal/bench); trace ids encode
// the run, so a run's spans are found in ClickHouse by prefix.
package main

import (
	"fmt"
	"os"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: loadgen thresholds|run|analyze|render|probe [flags]")
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "thresholds":
		err = thresholds(os.Args[2:], os.Stdout)
	case "run":
		err = run(os.Args[2:], os.Stdout)
	case "analyze":
		err = analyze(os.Args[2:], os.Stdout)
	case "render":
		err = render(os.Args[2:], os.Stdout)
	case "probe":
		err = probe(os.Args[2:], os.Stdout)
	default:
		err = fmt.Errorf("unknown command %q", os.Args[1])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "loadgen:", err)
		os.Exit(1)
	}
}
