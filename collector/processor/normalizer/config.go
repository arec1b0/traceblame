package normalizer

// Config of the normalizer processor. The stub has no settings yet; mapping (issue #8) and
// redaction (issue #9) add theirs.
type Config struct{}

// Validate checks the configuration.
func (*Config) Validate() error {
	return nil
}
