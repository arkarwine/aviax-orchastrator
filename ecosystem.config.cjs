module.exports = {
  apps: [
    {
      name: "world",
      script: "manager.py",
      interpreter: "python3",
      cwd: __dirname,
      treekill: false,
      autorestart: true,
      restart_delay: 3000,
      env: {
        MANAGER_PM2_TREEKILL_DISABLED: "1",
      },
    },
  ],
};
