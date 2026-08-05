Publish only a Complete Delivery report's rendered HTML/CSS bundle.

Usage:
  gc complete-delivery report publish \
    --source <delivery-report-directory> \
    --destination-root <curated-report-root> \
    [--slug <safe-report-slug>]

The publisher rejects unsafe slugs, symlinked sources/destinations, missing or
oversized files, and path escapes. It copies only `index.html` and
`styles.css`; workflow state JSON is never published.
