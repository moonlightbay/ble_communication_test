#bio-eeg_transmission_delay.csv
data <- read.csv("bio-eeg_transmission_delay.csv")

plot(data$Frame.Number, data$Delay..us., type='o', col='blue', xlab='Frame Number', ylab='Transmission Delay (us)', main='Transmission Delay vs Frame Number', cex.main=1.5, cex.lab=1.2, cex.axis=1.2) 

# Save the plot as a PNG file
dev.copy(png, 'bio-eeg_transmission_delay_plot.png', width=1960, height=1440)
dev.off()


