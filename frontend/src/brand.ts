// Which brand layer this build ships: '' for open source, 'kith-climate' for
// the hosted Kith Climate build (VITE_TRET_BRAND, see vite.config.ts).
export const BRAND: string = import.meta.env.VITE_TRET_BRAND ?? ''

// The kith-climate brand is light only.
export const LIGHT_ONLY = BRAND === 'kith-climate'
