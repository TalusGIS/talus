import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      // react-hooks v7's `recommended` preset also enables the React Compiler diagnostics
      // (refs-during-render, set-state-in-effect, purity, …) — ~90 findings against standing
      // idioms here (e.g. the map code's render-time ref mirrors). Adopting them is a deliberate
      // refactor decision, NOT part of a dependency bump — so only the two classic rules are
      // enforced, at their pre-v7 severities. Revisit if/when the React Compiler is adopted.
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],
      // Guard against the const-TDZ blank-route class (a useMemo/useEffect reading state declared
      // BELOW it crashes at render with tsc clean — it bit twice on 2026-07-02: non-spatial tables,
      // then map-rename). Positional, so the discipline is simply: declare state/refs above their
      // first textual use. `functions: false` keeps hoisted function declarations legal.
      '@typescript-eslint/no-use-before-define': [
        'error',
        {
          functions: false,
          classes: true,
          variables: true,
          enums: true,
          typedefs: false,
          ignoreTypeReferences: true,
        },
      ],
    },
  },
)
