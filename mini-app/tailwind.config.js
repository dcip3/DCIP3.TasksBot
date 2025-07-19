/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        tg: {
          bg: 'var(--tg-theme-bg-color)',
          text: 'var(--tg-theme-text-color)',
          hint: 'var(--tg-theme-hint-color)',
          link: 'var(--tg-theme-link-color)',
          button: 'var(--tg-theme-button-color)',
          'button-text': 'var(--tg-theme-button-text-color)',
          'secondary-bg': 'var(--tg-theme-secondary-bg-color)',
          'accent-text': 'var(--tg-theme-accent-text-color)',
          'destructive': 'var(--tg-theme-destructive-text-color)',
          'section-bg': 'var(--tg-theme-section-bg-color)',
          'section-header': 'var(--tg-theme-section-header-text-color)',
          'subtitle': 'var(--tg-theme-subtitle-text-color)',
        }
      },
      backgroundColor: {
        tg: {
          bg: 'var(--tg-theme-bg-color)',
          button: 'var(--tg-theme-button-color)',
          secondary: 'var(--tg-theme-secondary-bg-color)',
          section: 'var(--tg-theme-section-bg-color)',
          header: 'var(--tg-theme-header-bg-color)',
        }
      },
      textColor: {
        tg: {
          base: 'var(--tg-theme-text-color)',
          hint: 'var(--tg-theme-hint-color)',
          link: 'var(--tg-theme-link-color)',
          button: 'var(--tg-theme-button-text-color)',
          accent: 'var(--tg-theme-accent-text-color)',
          destructive: 'var(--tg-theme-destructive-text-color)',
          'section-header': 'var(--tg-theme-section-header-text-color)',
          subtitle: 'var(--tg-theme-subtitle-text-color)',
        }
      }
    },
  },
  plugins: [],
} 