## Description: <br>
Process Avanza CSV exports, calculate TWRR/Modified Dietz returns, and track portfolio performance. <br>

This skill is ready for commercial/non-commercial use. <br>

## Publisher: <br>
[patello](https://clawhub.ai/user/patello) <br>

### License/Terms of Use: <br>
MIT-0 <br>


## Use Case: <br>
Investors or agents managing a local Avanza portfolio use this skill to import CSV transaction exports, maintain portfolio data, and calculate account-level performance returns. <br>

### Deployment Geography for Use: <br>
Global <br>

## Known Risks and Mitigations: <br>
Risk: Sensitive portfolio data may be exposed if CSV exports or the local SQLite database are stored in shared or synced locations. <br>
Mitigation: Keep transaction files and the SQLite database in a private workspace, restrict filesystem access, and back up data before destructive operations. <br>
Risk: Optional price updates can send held asset names to Avanza. <br>
Mitigation: Use `--update-prices never` for offline workflows or when asset-name disclosure is not acceptable. <br>
Risk: Reset commands can clear the local database. <br>
Mitigation: Run reset only with explicit confirmation and keep a recent backup of the database and source CSV exports. <br>


## Reference(s): <br>
- [Workflows](references/workflows.md) <br>
- [Troubleshooting](references/troubleshooting.md) <br>


## Skill Output: <br>
**Output Type(s):** [Shell commands, Configuration, Guidance, Analysis] <br>
**Output Format:** [Markdown with inline shell commands and CLI output guidance] <br>
**Output Parameters:** [1D] <br>
**Other Properties Related to Output:** [May read Avanza CSV exports, write or update a local SQLite database, and optionally make Avanza price lookup requests.] <br>

## Skill Version(s): <br>
2.8.0 (source: server release evidence) <br>


## Ethical Considerations: <br>
Users should evaluate whether this skill is appropriate for their environment, review any generated or modified files before relying on them, and apply their organization's safety, security, and compliance requirements before deployment. <br>
