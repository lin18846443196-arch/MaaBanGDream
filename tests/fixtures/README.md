# Optional local replay evidence

Raw challenge/team/cooperative screenshots, account-visible recordings and local
device logs are intentionally excluded from Git. Template crops are tracked in
`resource/image`. Pure logic, synthetic frames and pipeline tests run without raw
recordings; tests that actually read a missing private screenshot are skipped with
an explicit reason. Local maintainers can restore the matching fixture directory
to run those replay tests. Skips are not a successful real-device acceptance.
