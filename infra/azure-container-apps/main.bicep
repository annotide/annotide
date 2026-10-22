// Annotide on Azure Container Apps, scaled to zero when idle (README.md).
//
// One Container App runs the whole platform as one replica: nginx (the
// frontend image), the API, the worker and Valkey as its queue. It scales
// between 0 and 1 replicas: up on HTTP traffic, on a backlog the worker has to
// process (worker-backlog.sql, counted in PostgreSQL by KEDA) and once a day
// for the licence refresh; down after five idle minutes. Valkey lives and dies
// with the replica: the `job` row is the queue's source of truth, and the app
// only scales in with no queued or running jobs.
//
// Next to it: PostgreSQL Flexible Server (Burstable B1ms, the only resource
// that bills while idle), a storage account for media and results, a
// user-assigned identity with access to it, and Log Analytics with a daily cap.
// No paid networking: PostgreSQL faces the internet behind its firewall
// (Azure services only, since consumption Container Apps have no fixed
// outbound address), a password and TLS.
//
// Build the ARM template the Deploy to Azure button uses after any change:
//   az bicep build --file main.bicep --outfile azuredeploy.json

@description('Name of the Container App and prefix of the other resources: lowercase letters and digits, starting with a letter.')
@minLength(2)
@maxLength(16)
param name string = 'annotide'

@description('Azure region. Defaults to the resource group\'s.')
param location string = resourceGroup().location

@description('Annotide release to run (the image tag on ghcr.io/annotide).')
param annotideVersion string = '0.3.0'

@description('Licence key from annotide.com. Empty: Community (up to three active users).')
@secure()
param licenseKey string = ''

@description('Generated on every deployment; re-running the template sets a new one on server and app together.')
@secure()
param postgresPassword string = newGuid()

var suffix = uniqueString(resourceGroup().id, name)
var backendImage = 'ghcr.io/annotide/annotide-backend:${annotideVersion}'
var frontendImage = 'ghcr.io/annotide/annotide-frontend:${annotideVersion}'
var valkeyImage = 'docker.io/valkey/valkey:9.1.2-alpine'
var postgresUser = 'annotide'
var databaseName = 'annotide'
var storageContainerName = 'annotide'
// Storage Blob Data Contributor
var blobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    // Logs are the one cost that grows by itself; a team install stays far below.
    workspaceCapping: { dailyQuotaGb: json('0.5') }
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-identity'
  location: location
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take('${name}${suffix}', 24)
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    // Entra only: the app signs browser URLs with user delegation SAS. The
    // account keys cannot read data, which is what lets key2 serve as the
    // app's secret key below.
    allowSharedKeyAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    // The browser reads and uploads media directly with signed URLs.
    cors: {
      corsRules: [
        {
          allowedOrigins: [ publicUrl ]
          allowedMethods: [ 'GET', 'HEAD', 'PUT', 'OPTIONS' ]
          allowedHeaders: [ '*' ]
          exposedHeaders: [ '*' ]
          maxAgeInSeconds: 3600
        }
      ]
    }
  }
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: storageContainerName
}

resource blobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, blobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataContributor)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${name}-pg-${suffix}'
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresUser
    administratorLoginPassword: postgresPassword
    storage: { storageSizeGB: 32 }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
  }
}

// The server takes one change at a time, so these run in sequence.
resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    value: 'CITEXT,PGCRYPTO'
    source: 'user-override'
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [ extensions ]
}

// 0.0.0.0 is Azure's "allow Azure services" rule: the app and the KEDA
// scaler have no fixed outbound address.
resource azureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
  dependsOn: [ database ]
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

var publicUrl = 'https://${name}.${environment.properties.defaultDomain}'
var postgresHost = postgres.properties.fullyQualifiedDomainName
var databaseUrl = 'postgresql+asyncpg://${postgresUser}:${postgresPassword}@${postgresHost}:5432/${databaseName}?ssl=require'
var kedaConnection = 'postgresql://${postgresUser}:${postgresPassword}@${postgresHost}:5432/${databaseName}?sslmode=require'

var licenseSecret = empty(licenseKey) ? [] : [ { name: 'license-key', value: licenseKey } ]
var licenseEnv = empty(licenseKey) ? [] : [ { name: 'APP_LICENSE_KEY', secretRef: 'license-key' } ]

var appEnv = concat(
  [
    { name: 'APP_ENV', value: 'production' }
    { name: 'APP_DATABASE_URL', secretRef: 'database-url' }
    { name: 'APP_REDIS_URL', value: 'redis://127.0.0.1:6379/0' }
    { name: 'APP_SECRET_KEY', secretRef: 'secret-key' }
    { name: 'APP_FRONTEND_URL', value: publicUrl }
    { name: 'APP_CORS_ORIGINS', value: publicUrl }
    { name: 'APP_INTERNAL_API_URL', value: 'http://127.0.0.1:8000' }
    { name: 'APP_INSTALL_ID', value: guid(resourceGroup().id, name) }
    { name: 'APP_LICENSE_SERVER_URL', value: 'https://licence.annotide.com' }
    { name: 'APP_LOG_FORMAT', value: 'json' }
    { name: 'APP_OTEL_ENABLED', value: 'false' }
    // The identity the azure_blob connector signs in as (identity type
    // "managed identity", client ID from the deployment outputs).
    { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
  ],
  licenseEnv
)

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      secrets: concat(
        [
          { name: 'database-url', value: databaseUrl }
          { name: 'keda-postgres', value: kedaConnection }
          // Key2 of the storage account above: 88 random characters that
          // survive re-deployments (a fresh key on every run would sign
          // everyone out and lose MFA enrolments) and that cannot read data
          // (shared key access is off). Regenerating key2 rotates the app's
          // key: set the old value as APP_SECRET_KEY_PREVIOUS first (README.md).
          { name: 'secret-key', value: storage.listKeys().keys[1].value }
          { name: 'nginx-conf', value: loadTextContent('nginx.conf') }
        ],
        licenseSecret
      )
    }
    template: {
      initContainers: [
        {
          name: 'migrate'
          image: backendImage
          command: [ 'alembic', 'upgrade', 'head' ]
          env: appEnv
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
        }
      ]
      containers: [
        {
          name: 'frontend'
          image: frontendImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          volumeMounts: [ { volumeName: 'nginx-conf', mountPath: '/etc/nginx/conf.d' } ]
          probes: [
            {
              // Traffic waits for the API behind nginx, not just nginx.
              type: 'Readiness'
              httpGet: { path: '/api/v1/ready', port: 8080 }
              initialDelaySeconds: 3
              periodSeconds: 5
              failureThreshold: 10
            }
          ]
        }
        {
          name: 'api'
          image: backendImage
          command: [ 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000' ]
          env: appEnv
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
        {
          name: 'worker'
          image: backendImage
          command: [ 'arq', 'app.worker.main.WorkerSettings' ]
          env: appEnv
          resources: { cpu: json('0.75'), memory: '1.5Gi' }
        }
        {
          name: 'valkey'
          image: valkeyImage
          // A queue of job ids, rebuilt from PostgreSQL: nothing to persist.
          args: [ '--bind', '127.0.0.1', '--save', '', '--appendonly', 'no' ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
        }
      ]
      volumes: [
        {
          name: 'nginx-conf'
          storageType: 'Secret'
          secrets: [ { secretRef: 'nginx-conf', path: 'default.conf' } ]
        }
      ]
      scale: {
        minReplicas: 0
        // One replica holds the queue (Valkey); a team install needs no more.
        maxReplicas: 1
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '20' } }
          }
          {
            // Awake while the worker has jobs, annotations to publish or
            // webhooks due, so a long export or tiling job is never cut off.
            name: 'backlog'
            custom: {
              type: 'postgresql'
              metadata: {
                query: loadTextContent('worker-backlog.sql')
                targetQueryValue: '1'
                activationTargetQueryValue: '0'
              }
              auth: [ { secretRef: 'keda-postgres', triggerParameter: 'connection' } ]
            }
          }
          {
            // Licence refresh (LIC-27) on installs nobody opens for days.
            name: 'daily'
            custom: {
              type: 'cron'
              metadata: {
                timezone: 'Etc/UTC'
                start: '0 3 * * *'
                end: '15 3 * * *'
                desiredReplicas: '1'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [ azureServices, blobAccess, container ]
}

@description('Open this address to use Annotide. The first request after an idle period takes a few seconds.')
output url string = publicUrl

@description('Container App name, for az containerapp commands.')
output appName string = app.name

@description('Storage account for an azure_blob connector (Settings → Connectors).')
output storageAccount string = storage.name

@description('Blob container created for media and results.')
output storageContainer string = storageContainerName

@description('Client ID for the connector\'s "managed identity" sign-in.')
output identityClientId string = identity.properties.clientId

output postgresHost string = postgresHost
