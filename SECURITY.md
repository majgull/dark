# Security

The runner takes a task and a model endpoint, has a language model write
code in a virtual machine (VM), and judges the result with tests the model
never sees. The model is untrusted: its output is code, and a VM runs that
code. This file states what the design isolates and what it does not.
Deployment values in the repository are examples; replace them with your
own.

## What each run isolates

Each run executes in a fresh VM, cloned from a template at the start and
destroyed at the end. The executor VM is the machine that runs the model's
code. A second, fresh VM, the staging VM, clones the branch the run pushed
and runs the hidden acceptance tests there. The executor never sees those
tests. The runner builds the acceptance archive on the runner host and
ships it to the staging VM alone. Both machines are stopped and destroyed
with their disks, and the per-VM firewall file and cloud-init snippets are
removed with them. The runner checks that every VM it launched is gone and
records a miss when a reap cannot confirm it.

Each VM's network is opened by a firewall rule whose policy is drop, with
one rule group named in the code: `agentfw`. That group is not defined in
this repository. The deployment must define it, and the deployment decides
what a run may reach. The intended rule allows the service host only, so
the model can reach the Git host and the model endpoint and little else. If
the deployment defines no such group, the VM has no egress; if the group is
defined loosely, the isolation is only as good as that rule.

The task and the injected program are written into the VM as a file its
root user reads, and the model code runs as root inside the VM. The VM is
the security boundary, not a user account inside it.

## What the VMs hold

Both VMs receive the same Git access token, the agent token. It is a token
for the executor user on the Git host. The deployment's bootstrap puts that
user in a team with write access to every repository of the work
organisation, plus issues, pull requests and releases. A model that reads
the token can therefore push to any repository in that organisation and
read and write its issues, not only the run's own repository. It cannot use
the admin token: that stays on the runner host and is never written into a
VM. Each program removes the token from the comments it posts. Treat
everything a VM can reach with the agent token as reachable by the model.

The staging VM additionally holds a one-time value, a nonce, that the
executor never sees. The runner accepts a staging verdict only from a
comment that carries that nonce and was created after the staging VM
started. This is what stops the executor from posting a false pass.

## What the host must still trust

The design does not remove trust from the deployment; it moves it. The
runner host holds the admin token, the ledger and the code, and it reaches
the VM host over ssh, so it must be trusted. The VM host and its template
must be trusted: a template can run anything the runner then runs. The Git
host must be trusted to enforce the token scopes above. The firewall group
definition must be trusted, because the code only names it. The hidden
acceptance archive and the nonce are built on the runner host, so their
integrity rests on the runner host. The model endpoint and the gate in
front of it are the deployment's own machines, and this project does not
isolate them from the runner.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Open a GitHub
security advisory on the repository instead: on the repository page, the
Security tab, then "Report a vulnerability". That report is private to the
maintainers until a fix is ready.
