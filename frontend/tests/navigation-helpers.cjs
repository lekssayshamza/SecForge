const React = require('react'), jsx = require('react/jsx-runtime'), router = require('react-router-dom')
const { load, base, icon, Avatar } = require('./batch-a-helpers.cjs')
function header(user, open = true) {
  let state = 0
  return load('src/components/SiteHeader.tsx', {
    ...base, react: { ...React, useState: () => [state++ === 1 && open, () => {}] },
    'react-router-dom': router, '../context/AuthContext': { useAuth: () => ({ user, logout() {} }) },
    '../lib/permissions': require('./permissions-helper.cjs'),
    '../hooks/useUnsavedChanges': { confirmUnsavedNavigation: () => true },
    './Logo': icon, './Icon': icon, './ThemeToggle': { default: () => jsx.jsx('button', { children: 'Theme' }) }, './Avatar': Avatar,
  }).default
}
module.exports = { header }
