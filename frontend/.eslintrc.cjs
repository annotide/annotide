module.exports = {
  root: true,
  env: { browser: true, es2022: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
  plugins: ['@typescript-eslint', 'react-hooks'],
  ignorePatterns: ['dist', 'node_modules', '*.cjs', '*.config.js'],
  rules: {
    '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
  },
  overrides: [
    {
      // Playwright fixtures are `async ({ ... }, use) => { await use(value) }`;
      // that `use` is not a React hook.
      files: ['e2e/**/*.ts', 'playwright.config.ts'],
      env: { node: true },
      rules: { 'react-hooks/rules-of-hooks': 'off' },
    },
  ],
}
