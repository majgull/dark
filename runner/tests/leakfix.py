"""A leaked-credential fixture, built at test time so no literal sits in the
tree for gitleaks (or a reader) to trip over. The shape is the example
finding in gitleaks' own README (its sidekiq rule):
https://github.com/gitleaks/gitleaks/blob/master/README.md
"""

RULE = "sidekiq"  # the start of the rule id gitleaks reports for this line


def sidekiq_line():
    """One shell line gitleaks' default rules report under the sidekiq rule."""
    name = "BUNDLE_ENTERPRISE__CONTRIBSYS__COM"
    value = "cafe" + "babe" + ":" + "dead" + "beef"
    return f"export {name}={value}\n"


def sidekiq_value_tail():
    """A piece of the value that must never appear in redacted output."""
    return "dead" + "beef"
