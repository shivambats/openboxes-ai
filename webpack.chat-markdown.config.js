const path = require('path');

module.exports = {
  mode: 'production',
  entry: './src/js/utils/chatMarkdown.js',
  output: {
    path: path.resolve(__dirname, 'sidecar/workflow_ui'),
    filename: 'chat-markdown.js',
    library: 'ChatMarkdown',
    libraryTarget: 'window',
  },
};
