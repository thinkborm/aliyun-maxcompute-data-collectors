# Copyright 1999-2019 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import subprocess
import time
import traceback
import threading

from concurrent.futures import ThreadPoolExecutor

from odps_sql_runner import OdpsSQLRunner
from hive_sql_runner import HiveSQLRunner
from data_validator import DataValidator
from utils import print_utils


class MigrationRunner:
    def __init__(self,
                 odps_data_carrier_dir,
                 table_mapping,
                 hms_thrift_addr,
                 datasource,
                 verbose,
                 num_of_partitions,
                 failover_file):
        self._odps_data_carrier_dir = odps_data_carrier_dir
        self._table_mapping = table_mapping
        self._hms_thrift_addr = hms_thrift_addr
        self._datasource = datasource
        self._metasource_specified_by_user = False
        self._verbose = verbose
        self._validate_only = False
        self._append = False
        self._num_of_partitions = num_of_partitions
        self._failover_file = failover_file

        self._failover_mode = False
        if self._failover_file is not None:
            self._failover_mode = True

        # scheduling properties
        self._dynamic_scheduling = False
        self._threshold = 10
        self._parallelism = 20

        # dir and paths
        self._timestamp = str(int(time.time()))
        self._meta_carrier_path = os.path.join(self._odps_data_carrier_dir, "bin", "meta-carrier")
        self._meta_carrier_input_path = os.path.join(self._odps_data_carrier_dir,
                                                     "tmp",
                                                     "meta_carrier_input_" + self._timestamp)
        self._meta_carrier_output_dir = os.path.join(self._odps_data_carrier_dir,
                                                     "tmp",
                                                     "meta_carrier_output_" + self._timestamp)
        self._meta_processor_path = os.path.join(self._odps_data_carrier_dir,
                                                 "bin",
                                                 "meta-processor")
        self._meta_processor_output_dir = os.path.join(self._odps_data_carrier_dir,
                                                       "tmp",
                                                       "meta_processor_output_" + self._timestamp)
        self._odps_log_root_dir = os.path.join(self._odps_data_carrier_dir,
                                               "log",
                                               self._timestamp,
                                               "odps")
        self._hive_log_root_dir = os.path.join(self._odps_data_carrier_dir,
                                               "log",
                                               self._timestamp,
                                               "hive")
        self._oss_log_root_dir = os.path.join(self._odps_data_carrier_dir,
                                              "log",
                                              self._timestamp,
                                              "oss")
        self._verify_log_root_dir = os.path.join(self._odps_data_carrier_dir,
                                                 "log",
                                                 self._timestamp,
                                                 "verify")
        self._succeed_job_list_path = os.path.join(self._odps_data_carrier_dir,
                                                   "succeed_%s.txt" % self._timestamp)
        self._failed_job_list_path = os.path.join(self._odps_data_carrier_dir,
                                                  "failed_%s.txt" % self._timestamp)

        # global executors
        self._global_hive_sql_runner = HiveSQLRunner(self._odps_data_carrier_dir,
                                                     self._parallelism,
                                                     self._verbose)
        self._global_odps_sql_runner = OdpsSQLRunner(self._odps_data_carrier_dir,
                                                     self._parallelism,
                                                     self._verbose)
        self._data_validator = DataValidator(self._odps_data_carrier_dir,
                                             self._parallelism,
                                             self._verbose)

        # status tracking
        self._jobs = []
        self._num_total_jobs = 0
        self._num_succeed_jobs = 0
        self._num_failed_jobs = 0
        # self._conn = None

        # TODO: very hack, remove later
        self._num_hive_jobs = 0
        self._num_hive_jobs_lock = threading.Lock()

    def _execute_command(self, cmd):
        # try:
        if self._verbose:
            print_utils.print_yellow("Executing %s\n" % cmd)
        sp = subprocess.Popen(cmd,
                              shell=True,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              encoding='utf-8')
        stdout, stderr = sp.communicate()
        if sp.returncode != 0:
            # raise Exception(
            #     "Execute %s failed, stdout: %s, stderr %s\n" % (cmd, stdout, stderr))
            print_utils.print_red("Execute %s failed, stdout: %s, stderr %s\n" % (cmd, stdout, stderr))
        return stdout, stderr
        # except Exception as e:
        #     print_utils.print_red(traceback.format_exc())
        #     raise e

    def _gather_metadata(self):
        print_utils.print_yellow("[Gathering metadata]\n")
        tables = []
        new_table_mapping = {}
        for hive_db, hive_tbl, hive_part_spec, table_config in self._table_mapping:
            if hive_part_spec is not None and hive_part_spec != "" and table_config is not None and table_config != "":
                tables.append("%s.%s(%s){%s}" % (hive_db, hive_tbl, hive_part_spec, table_config))
            elif hive_part_spec is not None and hive_part_spec != "":
                tables.append("%s.%s(%s)" % (hive_db, hive_tbl, hive_part_spec))
            elif table_config is not None and table_config != "":
                tables.append("%s.%s{%s}" % (hive_db, hive_tbl, table_config))
            else:
                tables.append("%s.%s" % (hive_db, hive_tbl))

            # HACK here
            mc_pjt, mc_tbl = self._table_mapping[(hive_db, hive_tbl, hive_part_spec, table_config)]
            new_table_mapping[(hive_db, hive_tbl)] = (mc_pjt, mc_tbl)
        # replace multiple
        with open(self._meta_carrier_input_path, "w") as fd:
            fd.write("\n".join(tables))

        print_utils.print_yellow("sh %s -u %s -config %s -o %s -np %s\n" % (self._meta_carrier_path,
                                                                          self._hms_thrift_addr,
                                                                          self._meta_carrier_input_path,
                                                                          self._meta_carrier_output_dir,
                                                                          self._num_of_partitions))
        self._execute_command("sh %s -u %s -config %s -o %s -np %s" % (self._meta_carrier_path,
                                                                       self._hms_thrift_addr,
                                                                       self._meta_carrier_input_path,
                                                                       self._meta_carrier_output_dir,
                                                                       self._num_of_partitions))
        os.unlink(self._meta_carrier_input_path)
        self._table_mapping = new_table_mapping
        print_utils.print_green("[Gathering metadata Done]\n")

    def _apply_oss_config(self):
        # TODO: hack, refactor later
        def _parse_oss_config():
            with open(os.path.join(self._odps_data_carrier_dir, "oss_config.ini")) as fd:
                oss_endpoint = None
                oss_bucket = None
                for line in fd.readlines():
                    line = line[: -1] if line.endswith("\n") else line
                    if line.startswith("end_point="):
                        oss_endpoint = line[len("end_point="):]
                    if line.startswith("bucket="):
                        oss_bucket = line[len("bucket="):]
            if oss_endpoint is None or oss_bucket is None:
                raise Exception("Invalid oss configuration")
            return oss_endpoint, oss_bucket

        # handle oss configs
        oss_endpoint, oss_bucket = _parse_oss_config()
        sed_oss_endpoint_cmd = "sed -i 's#\"ossEndpoint\": .*,#\"ossEndpoint\": \"%s\",#g' %s"
        sed_oss_bucket_cmd = "sed -i 's#\"ossBucket\": .*#\"ossBucket\": \"%s\"#g' %s"
        global_config_path = os.path.join(self._meta_carrier_output_dir, "global.json")
        self._execute_command(sed_oss_endpoint_cmd % (oss_endpoint, global_config_path))
        self._execute_command(sed_oss_bucket_cmd % (oss_bucket, global_config_path))

    def _apply_table_mapping(self):
        for hive_db, hive_tbl in self._table_mapping:
            odps_pjt, odps_tbl = self._table_mapping[(hive_db, hive_tbl)]
            sed_odps_pjt_cmd = ("sed -i "
                                "'s#\"odpsProjectName\": \"%s\"#\"odpsProjectName\": \"%s\"#g' %s")
            hive_db_config_path = os.path.join(self._meta_carrier_output_dir,
                                               hive_db,
                                               hive_db + ".json")
            self._execute_command(sed_odps_pjt_cmd % (hive_db, odps_pjt, hive_db_config_path))

            sed_odps_tbl_cmd = "sed -i 's#\"odpsTableName\": \"%s\"#\"odpsTableName\": \"%s\"#g' %s"
            hive_tbl_config_path = os.path.join(self._meta_carrier_output_dir,
                                                hive_db,
                                                "table_meta",
                                                hive_tbl + ".json")
            self._execute_command(sed_odps_tbl_cmd % (hive_tbl, odps_tbl, hive_tbl_config_path))

            if self._append:
                sed_drop_if_exists = "sed -i 's#\"dropIfExists\": true,#\"dropIfExists\": false,#g' %s"
                self._execute_command(sed_drop_if_exists % hive_tbl_config_path)

            # TODO: remove later
            # since SQL doesn't support org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat,
            # use org.apache.hadoop.mapred.TextOutputFormat to work around
            target = ("\"outputFormat\": "
                      "\"org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat\"")
            replacement = ("\"outputFormat\": "
                           "\"org.apache.hadoop.mapred.TextOutputFormat\"")
            sed_output_format_cmd = ("sed -i 's#%s#%s#g' %s" % (target,
                                                                replacement,
                                                                hive_tbl_config_path))
            self._execute_command(sed_output_format_cmd)

    def _process_metadata(self):
        print_utils.print_yellow("[Processing metadata]\n")
        print_utils.print_yellow("sh %s -i %s -o %s\n" % (self._meta_processor_path,
                                                        self._meta_carrier_output_dir,
                                                        self._meta_processor_output_dir))
        self._execute_command("sh %s -i %s -o %s" % (self._meta_processor_path,
                                                     self._meta_carrier_output_dir,
                                                     self._meta_processor_output_dir))
        print_utils.print_green("[Processing metadata done]\n")

    def _build_table(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        # parallelism set to 1 to avoid OTS conflicts
        odps_sql_runner = OdpsSQLRunner(self._odps_data_carrier_dir, 1, self._verbose)
        try:
            odps_ddl_dir = os.path.join(self._meta_processor_output_dir,
                                        hive_db,
                                        hive_tbl,
                                        "odps_ddl")
            odps_log_dir = os.path.join(self._odps_log_root_dir, hive_db, hive_tbl)
            # create table
            script_path = os.path.join(odps_ddl_dir, "create_table.sql")
            create_table_future = odps_sql_runner.execute_script(hive_db,
                                                                 hive_tbl,
                                                                 script_path,
                                                                 odps_log_dir,
                                                                 True)
            # wait for success
            create_table_future.result()

            # add partitions
            add_partition_futures = []
            scripts = os.listdir(odps_ddl_dir)
            for script in scripts:
                if "create_table.sql" == script:
                    continue
                script_path = os.path.join(odps_ddl_dir, script)
                add_partition_futures.append(odps_sql_runner.execute_script(hive_db,
                                                                            hive_tbl,
                                                                            script_path,
                                                                            odps_log_dir,
                                                                            True))
            # wait for success
            for future in add_partition_futures:
                future.result()
        except Exception as e:
            raise e
        finally:
            odps_sql_runner.stop()

    def _create_table_only(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        # parallelism set to 1 to avoid OTS conflicts
        odps_sql_runner = OdpsSQLRunner(self._odps_data_carrier_dir, 1, self._verbose)
        try:
            odps_ddl_dir = os.path.join(self._meta_processor_output_dir,
                                        hive_db,
                                        hive_tbl,
                                        "odps_ddl")
            odps_log_dir = os.path.join(self._odps_log_root_dir, hive_db, hive_tbl)
            # create table
            script_path = os.path.join(odps_ddl_dir, "create_table.sql")
            create_table_future = odps_sql_runner.execute_script(hive_db,
                                                                 hive_tbl,
                                                                 script_path,
                                                                 odps_log_dir,
                                                                 True)
            # wait for success
            create_table_future.result()
        except Exception as e:
            raise e
        finally:
            odps_sql_runner.stop()

    def _add_partition(self, hive_db, hive_tbl, odps_pjt, odps_tbl, failed_table_sql):
        # parallelism set to 1 to avoid OTS conflicts
        odps_sql_runner = OdpsSQLRunner(self._odps_data_carrier_dir, 1, self._verbose)
        try:
            odps_ddl_dir = os.path.join(self._meta_processor_output_dir,
                                        hive_db,
                                        hive_tbl,
                                        "odps_ddl")
            odps_log_dir = os.path.join(self._odps_log_root_dir, hive_db, hive_tbl)
            # add partitions
            add_partition_futures = []
            scripts = os.listdir(odps_ddl_dir)
            for script in scripts:
                if "create_table.sql" == script:
                    continue
                if self._failover_mode:
                    fileIndex = self.parse_file_index(script)
                    print_utils.print_red("### script: " + script + ", fileIndex: " + fileIndex)
                    if fileIndex not in failed_table_sql[(hive_db, hive_tbl)]:
                        continue
                script_path = os.path.join(odps_ddl_dir, script)
                add_partition_futures.append(odps_sql_runner.execute_script(hive_db,
                                                                            hive_tbl,
                                                                            script_path,
                                                                            odps_log_dir,
                                                                            True))
            # wait for success
            for future in add_partition_futures:
                future.result()
        except Exception as e:
            raise e
        finally:
            odps_sql_runner.stop()

    def _build_external_table(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        # parallelism set to 1 to avoid OTS conflicts
        odps_sql_runner = OdpsSQLRunner(self._odps_data_carrier_dir, 1, self._verbose)
        try:
            odps_external_ddl_dir = os.path.join(self._meta_processor_output_dir,
                                                 hive_db,
                                                 hive_tbl,
                                                 "odps_external_ddl")
            odps_log_dir = os.path.join(self._odps_log_root_dir, hive_db, hive_tbl)
            # create external table
            script_path = os.path.join(odps_external_ddl_dir, "create_table.sql")
            create_external_table_future = odps_sql_runner.execute_script(hive_db,
                                                                          hive_tbl,
                                                                          script_path,
                                                                          odps_log_dir,
                                                                          True)
            # wait for success
            create_external_table_future.result()

            # add partitions
            add_external_partition_futures = []
            scripts = os.listdir(odps_external_ddl_dir)
            for script in scripts:
                if "create_table.sql" == script:
                    continue
                script_path = os.path.join(odps_external_ddl_dir, script)
                add_external_partition_futures.append(odps_sql_runner.execute_script(hive_db,
                                                                                     hive_tbl,
                                                                                     script_path,
                                                                                     odps_log_dir,
                                                                                     True))
            # wait for success
            for future in add_external_partition_futures:
                future.result()
        except Exception as e:
            raise e
        finally:
            odps_sql_runner.stop()

    def _transfer_data_from_hive(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        try:
            hive_sql_dir = os.path.join(self._meta_processor_output_dir,
                                        hive_db,
                                        hive_tbl,
                                        "hive_udtf_sql")
            hive_sql_script_path = os.path.join(hive_sql_dir, "multi_partition", hive_tbl + ".sql")
            hive_log_dir = os.path.join(self._hive_log_root_dir, hive_db, hive_tbl)
            self._global_hive_sql_runner.execute_script(hive_db,
                                                        hive_tbl,
                                                        hive_sql_script_path,
                                                        hive_log_dir,
                                                        True).result()
        except Exception as e:
            raise e

    def _transfer_data_from_hive_with_table_split(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        try:
            hive_sql_dir = os.path.join(self._meta_processor_output_dir,
                                        hive_db,
                                        hive_tbl,
                                        "hive_udtf_sql",
                                        "multi_partition")
            scripts = os.listdir(hive_sql_dir)
            for script in scripts:
                hive_sql_script_path = os.path.join(hive_sql_dir, script)
                hive_log_dir = os.path.join(self._hive_log_root_dir, hive_db, hive_tbl)
                self._global_hive_sql_runner.execute_script(hive_db,
                                                            hive_tbl,
                                                            hive_sql_script_path,
                                                            hive_log_dir,
                                                            True).result()
        except Exception as e:
            raise e
    def _transfer_data_from_hive_with_table_split_file(self, hive_db, hive_tbl, odps_pjt, odps_tbl, hive_sql_dir, script):
        try:
            hive_sql_script_path = os.path.join(hive_sql_dir, script)
            hive_log_dir = os.path.join(self._hive_log_root_dir, hive_db, hive_tbl)
            self._global_hive_sql_runner.execute_script(hive_db,
                                                        hive_tbl,
                                                        hive_sql_script_path,
                                                        hive_log_dir,
                                                        True).result()
        except Exception as e:
            raise e


    def _transfer_data_from_oss(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        try:
            transfer_sql_dir = os.path.join(self._meta_processor_output_dir,
                                            hive_db,
                                            hive_tbl,
                                            "odps_oss_transfer_sql",
                                            "single_partition")
            if not os.path.isdir(transfer_sql_dir):
                transfer_sql_dir = os.path.join(self._meta_processor_output_dir,
                                                hive_db,
                                                hive_tbl,
                                                "odps_oss_transfer_sql",
                                                "multi_partition")

            oss_log_dir = os.path.join(self._oss_log_root_dir, hive_db, hive_tbl)
            futures = []
            for script_name in os.listdir(transfer_sql_dir):
                script_path = os.path.join(transfer_sql_dir, script_name)
                future = self._global_odps_sql_runner.execute_script(hive_db,
                                                                     hive_tbl,
                                                                     script_path,
                                                                     oss_log_dir,
                                                                     False)
                futures.append(future)

            for future in futures:
                future.result()

        except Exception as e:
            raise e

    def _validate_data(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        try:
            hive_verify_sql_path = os.path.join(self._meta_processor_output_dir,
                                                hive_db,
                                                hive_tbl,
                                                "hive_verify_sql",
                                                hive_tbl + ".sql")

            odps_verify_sql_path = os.path.join(self._meta_processor_output_dir,
                                                hive_db,
                                                hive_tbl,
                                                "odps_verify_sql",
                                                hive_tbl + ".sql")
            if not self._data_validator.verify(hive_db,
                                               hive_tbl,
                                               odps_pjt,
                                               odps_tbl,
                                               hive_verify_sql_path,
                                               odps_verify_sql_path,
                                               self._verify_log_root_dir):
                raise Exception("Data validation failed")
        except Exception as e:
            raise e

    def _migrate_from_hive(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        self._build_table(hive_db, hive_tbl, odps_pjt, odps_tbl)
        self._increase_num_hive_jobs()
        self._transfer_data_from_hive(hive_db, hive_tbl, odps_pjt, odps_tbl)
        # self._validate_data(hive_db, hive_tbl, odps_pjt, odps_tbl)
        self._decrease_num_hive_jobs()


    def _migrate_from_hive_with_table_split(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        # failover, mode
        self._create_table_only(hive_db, hive_tbl, odps_pjt, odps_tbl)
        # full table, none-failover mode
        self._add_partition(hive_db, hive_tbl, odps_pjt, odps_tbl, None)
        self._increase_num_hive_jobs()
        self._transfer_data_from_hive_with_table_split(hive_db, hive_tbl, odps_pjt, odps_tbl)
        # self._validate_data(hive_db, hive_tbl, odps_pjt, odps_tbl)
        self._decrease_num_hive_jobs()


    def _migrate_from_oss(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        self._build_table(hive_db, hive_tbl, odps_pjt, odps_tbl)
        self._build_external_table(hive_db, hive_tbl, odps_pjt, odps_tbl)
        self._transfer_data_from_oss(hive_db, hive_tbl, odps_pjt, odps_tbl)

    def _validate(self, hive_db, hive_tbl, odps_pjt, odps_tbl):
        self._validate_data(hive_db, hive_tbl, odps_pjt, odps_tbl)

    def _increase_num_hive_jobs(self):
        with self._num_hive_jobs_lock:
            self._num_hive_jobs += 1

    def _decrease_num_hive_jobs(self):
        with self._num_hive_jobs_lock:
            self._num_hive_jobs -= 1

    def _report_progress(self):
        progress_format = ("[Progress][%.2f%%] waiting: %d, running: %d, (sub)running: %d, "
                           "succeed: %d, failed: %d, total: %d\n")
        while not (self._num_succeed_jobs + self._num_failed_jobs == self._num_jobs):
            progress = ((self._num_succeed_jobs + self._num_failed_jobs) / self._num_jobs) * 100
            num_waiting_jobs = (self._num_jobs - self._num_hive_jobs - self._num_succeed_jobs
                                - self._num_failed_jobs)
            print_utils.print_yellow(progress_format % (progress,
                                                        num_waiting_jobs,
                                                        self._num_hive_jobs,
                                                        len(self._jobs),
                                                        self._num_succeed_jobs,
                                                        self._num_failed_jobs,
                                                        self._num_jobs))
            time.sleep(10)

    def _wait(self, decider):
        while not decider():
            succeed, failed = self._handle_finished_jobs()
            self._num_succeed_jobs += succeed
            self._num_failed_jobs += failed
            time.sleep(10)

    def _can_submit(self):
        if self._dynamic_scheduling:
            stdout, _ = self._execute_command("/usr/lib/hadoop-current/bin/mapred job -list")
            m = re.search("Total jobs:(.*)\n", stdout)
            if m is not None:
                try:
                    return int(m.group(1)) + len(self._jobs) - self._num_hive_jobs < self._threshold
                except Exception as e:
                    traceback.print_exc()
                    pass

        return len(self._jobs) < self._parallelism

    def _can_terminate(self):
        return len(self._jobs) <= 0

    def _handle_finished_jobs(self):
        num_succeed_job = 0
        num_failed_job = 0
        for job in self._jobs:
            hive_db, hive_tbl, odps_pjt, odps_tbl, sqlName, future = job
            mc_pjt, mc_tbl = self._table_mapping[(hive_db, hive_tbl)]
            if future.done():
                self._jobs.remove(job)
                try:
                    future.result()
                    if sqlName == "validate_data" or sqlName == "migrate":
                        num_succeed_job += 1
                        print_utils.print_green("[SUCCEED] %s.%s -> %s.%s\n" % (hive_db,
                                                                                hive_tbl,
                                                                                mc_pjt,
                                                                                mc_tbl))
                        with open(self._succeed_job_list_path, 'a') as fd:
                            fd.write("%s.%s:%s.%s\n" % (hive_db, hive_tbl, odps_pjt, odps_tbl))
                except Exception as e:
                    num_failed_job += 1
                    print_utils.print_red("[FAILED] %s.%s -> %s.%s\n" % (hive_db,
                                                                         hive_tbl,
                                                                         mc_pjt,
                                                                         mc_tbl))
                    print_utils.print_red(traceback.format_exc())
                    with open(self._failed_job_list_path, 'a') as fd:
                        if sqlName == "create_table" or sqlName == "add_partition" or sqlName == "validate_data" or sqlName == "migrate":
                            fd.write("#%s.%s:%s.%s|%s\n" % (hive_db, hive_tbl, odps_pjt, odps_tbl, sqlName))
                        else:
                            fd.write("%s.%s:%s.%s|%s\n" % (hive_db, hive_tbl, odps_pjt, odps_tbl, sqlName))
        return num_succeed_job, num_failed_job

    def set_dynamic_scheduling(self):
        if os.path.exists("/usr/lib/hadoop-current/bin/mapred"):
            self._dynamic_scheduling = True
        else:
            msg = ("[ERROR] Failed to turn on dynamic scheduling, "
                   "file \'/usr/lib/hadoop-current/bin/mapred\' not found")
            raise Exception(msg)

    def set_threshold(self, threshold):
        self._threshold = threshold

    def set_parallelism(self, parallelism):
        self._parallelism = parallelism

    def set_metasource(self, metasource):
        self._meta_carrier_output_dir = metasource
        self._metasource_specified_by_user = True

    def set_validate_only(self):
        self._validate_only = True

    def set_append(self):
        self._append = True

    def parse_failover_file(self, failover_file_path):
        #dma_demo.inventory:ODPS_DATA_CARRIER_TEST.inventory|inventory_0.sql
        def parse_line(line: str):
            try:
                colon_idx = line.index(":")
            except ValueError as e:
                raise Exception("Cannot parse line: " + line)

            hive, mc = line[: colon_idx], line[colon_idx + 1:]

            try:
                dot_idx = hive.index(".")
            except ValueError as e:
                raise Exception("Cannot parse line: " + line)
            hive_db, hive_tbl = hive[: dot_idx].strip(), hive[dot_idx + 1:].strip()

            try:
                dot_idx = mc.index(".")
            except ValueError as e:
                raise Exception("Cannot parse line: " + line)
            mc_pjt, mc_tbl_file = mc[: dot_idx].strip(), mc[dot_idx + 1:].strip()

            try:
                split_idx = mc_tbl_file.index("|")
            except ValueError as e:
                raise Exception("Cannot parse line: " + line)
            mc_tbl, sqlFile = mc_tbl_file[: split_idx].strip(), mc_tbl_file[split_idx + 1:].strip()
            return hive_db, hive_tbl, mc_pjt, mc_tbl, sqlFile

        failed_table_mapping = {}
        with open(failover_file_path, "r") as fd:
            for line in fd.readlines():
                if line[0] == "#":
                    continue
                (hive_db, hive_tbl, mc_pjt, mc_tbl, sqlFile) = parse_line(line)
            fileIndex = self.parse_file_index(sqlFile)
            if (hive_db, hive_tbl) not in failed_table_mapping.keys():
                failed_table_mapping[(hive_db, hive_tbl)] = {fileIndex}
            else:
                failed_table_mapping[(hive_db, hive_tbl)].add(fileIndex)
        return failed_table_mapping

    def parse_file_index(self, fileName):
        # create_partition_0.sql
        # inventory_0.sql
        try:
            dot_idx = fileName.index(".")
        except ValueError as e:
            raise Exception("Cannot parse line: " + line)
        name, type = fileName[: dot_idx].strip(), fileName[dot_idx + 1:].strip()
        fileIndex = name[(name.rindex("_") + 1):]
        return fileIndex

    def run(self):
        # TODO: add scheduling module
        # TODO: use sqlite to track migration status, support resuming

        if not self._metasource_specified_by_user and not self._validate_only:
            self._gather_metadata()

        self._num_jobs = len(self._table_mapping)

        if not self._validate_only:
            self._apply_oss_config()
            self._apply_table_mapping()
            self._process_metadata()

        print_utils.print_yellow("[Migration starts]\n")
        executor = ThreadPoolExecutor(self._parallelism)
        progress_reporter = threading.Thread(target=self._report_progress)
        progress_reporter.start()
        try:
            for hive_db, hive_tbl in self._table_mapping:
                odps_pjt, odps_tbl = self._table_mapping[(hive_db, hive_tbl)]

                # wait for available slot
                self._wait(self._can_submit)

                if self._validate_only:
                    migrate = self._validate
                elif self._datasource == "Hive":
                    if int(self._num_of_partitions) > 0:

                        failed_table_sql = {}
                        if self._failover_mode:
                            failed_table_sql = self.parse_failover_file(self._failover_file)

                        ## split _migrate_from_hive_with_table_split into several steps.
                        # migrate = self._migrate_from_hive_with_table_split

                        if not self._failover_mode:
                            # step1. create table
                            future = executor.submit(self._create_table_only, hive_db, hive_tbl, odps_pjt, odps_tbl)
                            self._jobs.append((hive_db, hive_tbl, odps_pjt, odps_tbl, "create_table", future))
                            while True:
                                if future.done():
                                    break

                        # step2. add partition
                        future = executor.submit(self._add_partition, hive_db, hive_tbl, odps_pjt, odps_tbl, failed_table_sql)
                        self._jobs.append((hive_db, hive_tbl, odps_pjt, odps_tbl, "add_partition", future))
                        while True:
                            if future.done():
                                break

                        # step3. transfer data
                        self._increase_num_hive_jobs()

                        hive_sql_dir = os.path.join(self._meta_processor_output_dir,
                                                    hive_db,
                                                    hive_tbl,
                                                    "hive_udtf_sql",
                                                    "multi_partition")
                        scripts = os.listdir(hive_sql_dir)

                        for script in scripts:
                            if self._failover_mode:
                                fileIndex = self.parse_file_index(script)
                                if fileIndex not in failed_table_sql[(hive_db, hive_tbl)]:
                                    continue
                            future = executor.submit(self._transfer_data_from_hive_with_table_split_file,
                                                     hive_db, hive_tbl, odps_pjt, odps_tbl, hive_sql_dir, script)
                            self._jobs.append((hive_db, hive_tbl, odps_pjt, odps_tbl, script, future))
                            while True:
                                if future.done():
                                    break

                        # step4. validate data
                        future = executor.submit(self._validate_data, hive_db, hive_tbl, odps_pjt, odps_tbl)
                        self._jobs.append((hive_db, hive_tbl, odps_pjt, odps_tbl, "validate_data", future))
                        while True:
                            if future.done():
                                break

                        self._decrease_num_hive_jobs()

                    else:
                        migrate = self._migrate_from_hive
                elif self._datasource == "OSS":
                    migrate = self._migrate_from_oss
                else:
                    raise Exception("Unsupported datasource")

                if int(self._num_of_partitions) == 0:
                    future = executor.submit(migrate,
                                             hive_db,
                                             hive_tbl,
                                             odps_pjt,
                                             odps_tbl)
                    self._jobs.append((hive_db, hive_tbl, odps_pjt, odps_tbl, "migrate", future))

            self._wait(self._can_terminate)
            progress_reporter.join()
        except Exception as e:
            traceback.print_exc()
            print_utils.print_red("[Migration failed]" + e + "\n")
        # finally:
        #     executor.shutdown()
        print_utils.print_green("[Migration done]\n")

    def stop(self):
        self._global_hive_sql_runner.stop()
        self._data_validator.stop()
