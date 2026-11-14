// Copy into the deployed OHIF 3 application's config (APP_CONFIG).
// Put OHIF and Django behind one same-origin reverse proxy. Do not enable
// unauthenticated CORS or STOW uploads; the clinic API owns ingestion.
window.config = {
  routerBasename: '/ohif',
  extensions: [],
  modes: [],
  showStudyList: true,
  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
      sourceName: 'clinicDICOMweb',
      configuration: {
        friendlyName: 'Authenticated clinic imaging',
        name: 'Clinic DICOMweb',
        qidoRoot: `${window.location.origin}/api/v1/dicomweb`,
        wadoRoot: `${window.location.origin}/api/v1/dicomweb`,
        wadoUriRoot: `${window.location.origin}/api/v1/dicomweb`,
        qidoSupportsIncludeField: true,
        imageRendering: 'wadors',
        thumbnailRendering: 'wadors',
        enableStudyLazyLoad: true,
        supportsFuzzyMatching: false,
        supportsWildcard: false,
        // All ingestion remains in the clinic API; this viewer is read-only.
        dicomUploadEnabled: false,
      },
    },
  ],
  defaultDataSourceName: 'clinicDICOMweb',
};
