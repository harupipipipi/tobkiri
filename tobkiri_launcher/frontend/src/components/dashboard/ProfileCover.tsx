import {useId} from 'react';

/** Stable, local artwork inspired by the selected Fable card; no remote assets. */
export function ProfileCover({profileId}: {profileId: string}) {
  const id = useId();
  const seed = [...profileId].reduce((hash, character) => (
    (Math.imul(hash, 31) + character.codePointAt(0)!) >>> 0
  ), 0);
  const cut = 120 + seed % 70;
  const hue = 220 + seed % 30;

  return (
    <svg aria-hidden="true" focusable="false" viewBox="0 0 320 180" preserveAspectRatio="xMidYMid slice" className="absolute inset-0 h-full w-full">
      <defs>
        <linearGradient id={`${id}-base`} x2="1" y2="1">
          <stop stopColor={`hsl(${hue} 28% 28%)`} />
          <stop offset="1" stopColor={`hsl(${hue} 25% 15%)`} />
        </linearGradient>
        <linearGradient id={`${id}-cut`} x1="0" y1="1" x2="1" y2="0">
          <stop stopColor="#a5b4fc" />
          <stop offset="1" stopColor="#bca48d" />
        </linearGradient>
        <pattern id={`${id}-dots`} width="20" height="20" patternUnits="userSpaceOnUse">
          <circle cx="2" cy="2" r="1" fill="white" fillOpacity="0.16" />
        </pattern>
      </defs>
      <path fill={`url(#${id}-base)`} d="M0 0h320v180H0z" />
      <path fill={`url(#${id}-dots)`} d="M0 0h320v180H0z" />
      <path fill={`url(#${id}-cut)`} opacity="0.9" d={`M${cut} 180l55-180h34l-55 180z`} />
      <path fill="white" opacity="0.09" d={`M${cut + 57} 180l55-180h15l-55 180z`} />
      <circle cx="272" cy="38" r="90" fill="none" stroke="white" strokeOpacity="0.2" />
      <circle cx="272" cy="38" r="50" fill="none" stroke="white" strokeOpacity="0.16" strokeDasharray="3 6" />
    </svg>
  );
}
