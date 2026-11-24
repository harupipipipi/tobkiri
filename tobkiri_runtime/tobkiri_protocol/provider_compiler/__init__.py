"""Pure provider payload contracts shared across Pack boundaries.

These helpers validate serialized connection policies and compile request
parameters. They do not resolve connection authority, access credentials,
perform discovery, or dispatch requests; those remain Pack/Host operations.
"""
