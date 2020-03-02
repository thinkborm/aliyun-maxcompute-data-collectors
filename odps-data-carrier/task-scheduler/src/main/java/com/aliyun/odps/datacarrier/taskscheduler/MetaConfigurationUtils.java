package com.aliyun.odps.datacarrier.taskscheduler;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.LinkedList;
import java.util.List;

import com.aliyun.odps.datacarrier.taskscheduler.MetaConfiguration.*;

import static com.aliyun.odps.datacarrier.taskscheduler.Constants.META_CONFIG_FILE;
import static com.aliyun.odps.datacarrier.taskscheduler.Constants.ODPS_DATA_CARRIER;

public class MetaConfigurationUtils {

  private static final List<String> hiveJdbcExtraSettings = new ArrayList<String>() {
    {
      add("hive.fetch.task.conversion=none");
      add("hive.execution.engine=mr");
      add("mapreduce.job.name=data-carrier");
      add("mapreduce.max.split.size=512000000");
      add("mapreduce.task.timeout=3600000");
      add("mapreduce.map.maxattempts=0");
      add("mapred.map.tasks.speculative.execution=false");
    }
  };

  public static MetaConfiguration readConfigFile(File configFile) throws IOException {
    if (!configFile.exists()) {
      throw new RuntimeException("Config file not exists (yet)");
    }
    FileInputStream inputStream = new FileInputStream(configFile);
    String content = new String(org.apache.commons.io.IOUtils.toByteArray(inputStream));
    return GsonUtils.getFullConfigGson().fromJson(content, MetaConfiguration.class);
  }

  public static MetaConfiguration generateSampleMetaConfiguration() {
    MetaConfiguration metaConfiguration = new MetaConfiguration("Jerry", "TestMigrationJob", DataSource.Hive);

    HiveConfiguration hiveConfiguration =
        new HiveConfiguration("jdbc:hive2://127.0.0.1:10000/default",
                              "Hive",
                              "",
                              "thrift://127.0.0.1:9083",
                              "",
                              "",
                              new String[]{}, hiveJdbcExtraSettings);
    metaConfiguration.setHiveConfiguration(hiveConfiguration);

    OdpsConfiguration odpsConfiguration = new OdpsConfiguration("", "", "", "", "");
    metaConfiguration.setOdpsConfiguration(odpsConfiguration);

    List<TableGroup> tablesGroupList = new ArrayList<>();
    TableGroup tablesGroup = new TableGroup();
    List<TableConfig> tables = new ArrayList<>();

    Config tableConfig = new Config(null, null, 10, 5, "");
    TableConfig table = new TableConfig("SourceDataBase", "SourceTable", "DestProject", "DestTable", tableConfig);
    tables.add(table);

    tablesGroup.setTables(tables);
    tablesGroup.setGroupConfig(tableConfig);

    tablesGroupList.add(tablesGroup);
    metaConfiguration.setTableGroups(tablesGroupList);
    metaConfiguration.setGlobalTableConfig(tableConfig);

    return metaConfiguration;
  }

  public static String getSampleMetaConfigurationStr() {
    return GsonUtils.getFullConfigGson().toJson(generateSampleMetaConfiguration(), MetaConfiguration.class);
  }

  public static File getDefaultConfigFile() {
    // TODO: use a fixed parent directory
    String currentDir = System.getProperty("user.dir");
    return new File(currentDir + "/" + META_CONFIG_FILE);
  }

  public static void generateConfigFile(File configFile) throws Exception {
    Files.deleteIfExists(configFile.toPath());
    configFile.createNewFile();
    FileOutputStream outputStream = new FileOutputStream(configFile);
    outputStream.write(getSampleMetaConfigurationStr().getBytes());
    outputStream.close();
  }

  public static void main(String[] args) throws Exception {
    File configFile = getDefaultConfigFile();
    generateConfigFile(configFile);
  }

}
