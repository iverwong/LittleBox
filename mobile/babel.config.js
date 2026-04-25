module.exports = function (api) {
  api.cache(true)
  return {
    presets: ['babel-preset-expo'],
    plugins: [
      'react-native-worklets/plugin', // 必须放 plugins 最末位
    ],
  }
}
