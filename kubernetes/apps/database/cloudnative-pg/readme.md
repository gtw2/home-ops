# cloudnative-pg

## S3 Configuration

Backups go to Garage through the Barman Cloud plugin (`cluster/objectstore.yaml`):
the `postgresql` bucket, with the `cnpg-key` access key stored in the 1Password
item `garage-cnpg`. Creating the bucket and key is covered in
[the Garage readme](../../storage/garage/readme.md#buckets-and-keys).
