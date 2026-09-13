# Tobkiri Repository Context Pack

Uses the generic Subagent Placement compiler to run a bounded, low-cost
repository context scout before a stronger caller model investigates.

The Pack lists and reads only workspace-jailed text candidates. Generated,
dependency, binary, oversized, credential-named, and secret-like files are
discarded before model calls. Remaining files are processed in bounded map
batches and reduced to a provenance-bearing Repository Evidence bundle.

The result is a Tool response, so the caller model receives relevant file
paths, summaries, exact evidence excerpts, hashes, exclusions, and statistics
without loading the entire repository into its context.
