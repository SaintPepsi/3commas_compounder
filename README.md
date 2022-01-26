# 3commas_compounder
Script to autocompound 3commas BO:SO based on user provided risk factor
# Setup
### Step 1
`git clone` this repo into your working directory
### Step 2
Input keys into the `config.ini.example` file and rename it as `config.ini`
### Step 3
`docker-compose up -d`
### Step 4
Follow prompts
### Step 5 (optional for automating)
 Example of a bash script you can put on schedule (cron)
```
#!/bin/sh

cd <your/working/directory/where/git/clone>
docker-compose run --name 3c_compound_job autocompound3c
docker-compose down
```