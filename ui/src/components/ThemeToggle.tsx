import { useTheme } from "../theme/theme";

// Day / night switch for the header. Day is the default; the choice is
// remembered per viewer (src/theme/theme.ts).
export default function ThemeToggle() {
  const [theme, setTheme] = useTheme();
  const night = theme === "dark";
  const label = night ? "Switch to day mode" : "Switch to night mode";
  return (
    <button
      type="button"
      onClick={() => setTheme(night ? "light" : "dark")}
      aria-label={label}
      aria-pressed={night}
      title={label}
      className="w-8 h-8 flex items-center justify-center rounded text-gray-400 hover:text-gray-100 hover:bg-gray-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 transition-colors"
    >
      {night ? (
        // Sun: tapping returns to day.
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
        </svg>
      ) : (
        // Moon: tapping switches to night.
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  );
}
